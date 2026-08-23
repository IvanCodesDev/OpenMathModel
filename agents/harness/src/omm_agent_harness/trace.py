"""TraceHub: minimal run tracing (design §4.6).

Span hierarchy ``run → node → turn → call``, concept-aligned with OTel but
carried by structured logs and an in-memory tree — no SDK dependency (the
MVP decision in §4.6). LLM calls are recorded with the same fact shape as
the ``TOOL_CALLED{tool:"llm.chat"}`` audit event so the trace, the event
log and the usage ledger never disagree about what happened.

``export_markdown`` renders the run report evals attach to their records.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Span", "TraceHub"]

_KINDS = ("run", "node", "turn", "call")


@dataclass
class Span:
    span_id: str
    kind: str  # "run" | "node" | "turn" | "call"
    name: str
    parent_id: str | None
    started_at: float
    ended_at: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at) * 1000)


class TraceHub:
    def __init__(
        self,
        run_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_id = run_id
        self._clock = clock
        self._logger = logger or logging.getLogger("omm.trace")
        self._spans: list[Span] = []
        self._stack: list[Span] = []
        self._counter = 0
        root = self._open("run", run_id)
        self._stack.append(root)

    # -- spans -------------------------------------------------------------------

    def _open(self, kind: str, name: str, **attrs: Any) -> Span:
        if kind not in _KINDS:
            raise ValueError(f"unknown span kind {kind!r}")
        self._counter += 1
        span = Span(
            span_id=f"sp_{self._counter:04d}",
            kind=kind,
            name=name,
            parent_id=self._stack[-1].span_id if self._stack else None,
            started_at=self._clock(),
            attrs=dict(attrs),
        )
        self._spans.append(span)
        return span

    @contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[Span]:
        span = self._open(kind, name, **attrs)
        self._stack.append(span)
        try:
            yield span
        finally:
            span.ended_at = self._clock()
            self._stack.pop()
            self._logger.info(
                "trace %s %s/%s %sms",
                self._run_id,
                span.kind,
                span.name,
                span.duration_ms,
                extra={"span": span.span_id, "attrs": span.attrs},
            )

    # -- llm call audit (shape mirrors TOOL_CALLED{tool="llm.chat"}) --------------

    def record_llm_call(self, record: dict[str, Any]) -> None:
        """Accepts the gateway's usage-listener record as-is."""
        span = self._open("call", str(record.get("prompt_id") or "llm.chat"), **record)
        span.ended_at = span.started_at + (record.get("duration_ms") or 0) / 1000.0
        self._logger.info(
            "trace %s llm.chat prompt=%s model=%s tokens=%s+%s %sms",
            self._run_id,
            record.get("prompt_id"),
            record.get("model"),
            record.get("prompt_tokens"),
            record.get("completion_tokens"),
            record.get("duration_ms"),
        )

    # -- reporting -----------------------------------------------------------------

    def totals(self) -> dict[str, Any]:
        calls = [s for s in self._spans if s.kind == "call" and "prompt_tokens" in s.attrs]
        return {
            "llm_calls": len(calls),
            "prompt_tokens": sum(int(s.attrs.get("prompt_tokens", 0)) for s in calls),
            "completion_tokens": sum(
                int(s.attrs.get("completion_tokens", 0)) for s in calls
            ),
        }

    def export_markdown(self) -> str:
        """Run report: span tree with durations plus token totals."""
        by_parent: dict[str | None, list[Span]] = {}
        for span in self._spans:
            by_parent.setdefault(span.parent_id, []).append(span)

        lines: list[str] = [f"# Run report: {self._run_id}", ""]

        def walk(parent_id: str | None, depth: int) -> None:
            for span in by_parent.get(parent_id, []):
                duration = f"{span.duration_ms}ms" if span.duration_ms is not None else "…"
                indent = "  " * depth
                extra = ""
                if span.kind == "call" and "model" in span.attrs:
                    extra = (
                        f" · model={span.attrs.get('model')}"
                        f" tokens={span.attrs.get('prompt_tokens', 0)}"
                        f"+{span.attrs.get('completion_tokens', 0)}"
                    )
                lines.append(f"{indent}- **{span.kind}** {span.name} ({duration}){extra}")
                walk(span.span_id, depth + 1)

        roots = by_parent.get(None, [])
        for root in roots:
            duration = f"{root.duration_ms}ms" if root.duration_ms is not None else "…"
            lines.append(f"- **{root.kind}** {root.name} ({duration})")
            walk(root.span_id, 1)

        totals = self.totals()
        lines += [
            "",
            "## Totals",
            f"- llm_calls: {totals['llm_calls']}",
            f"- prompt_tokens: {totals['prompt_tokens']}",
            f"- completion_tokens: {totals['completion_tokens']}",
        ]
        return "\n".join(lines)

    def close(self) -> None:
        """End the root span (idempotent)."""
        root = self._spans[0]
        if root.ended_at is None:
            root.ended_at = self._clock()
