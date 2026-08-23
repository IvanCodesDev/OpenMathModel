"""TraceHub: span nesting, llm-call audit shape, markdown report."""

from __future__ import annotations

import pytest
from omm_agent_harness.trace import TraceHub


class SteppingClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.1
        return self.now


def make_hub() -> TraceHub:
    return TraceHub("run_0001", clock=SteppingClock())


def test_span_nesting_follows_run_node_turn_call():
    hub = make_hub()
    with hub.span("node", "problem_analysis") as node:
        with hub.span("turn", "turn-1") as turn:
            with hub.span("call", "llm.chat") as call:
                pass

    assert node.parent_id is not None  # under the run root
    assert turn.parent_id == node.span_id
    assert call.parent_id == turn.span_id
    assert call.duration_ms is not None and call.duration_ms > 0


def test_unknown_span_kind_fails_fast():
    hub = make_hub()
    with pytest.raises(ValueError, match="unknown span kind"):
        with hub.span("phase", "x"):
            pass


def test_record_llm_call_feeds_totals():
    hub = make_hub()
    for tokens in ((100, 20), (50, 5)):
        hub.record_llm_call(
            {
                "tool": "llm.chat",
                "prompt_id": "model_planning.default",
                "model": "m-strong",
                "prompt_hash": "abc",
                "prompt_tokens": tokens[0],
                "completion_tokens": tokens[1],
                "duration_ms": 12,
                "replayed": False,
            }
        )

    totals = hub.totals()
    assert totals == {"llm_calls": 2, "prompt_tokens": 150, "completion_tokens": 25}


def test_markdown_report_contains_tree_and_totals():
    hub = make_hub()
    with hub.span("node", "problem_analysis"):
        hub.record_llm_call(
            {
                "tool": "llm.chat",
                "prompt_id": "problem_analysis.default",
                "model": "m-fast",
                "prompt_hash": "abc",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "duration_ms": 5,
                "replayed": True,
            }
        )
    hub.close()

    report = hub.export_markdown()
    assert "# Run report: run_0001" in report
    assert "- **run** run_0001" in report
    assert "  - **node** problem_analysis" in report
    assert "model=m-fast tokens=10+2" in report
    assert "- llm_calls: 1" in report
    assert "- prompt_tokens: 10" in report


def test_close_is_idempotent():
    hub = make_hub()
    hub.close()
    first = hub.export_markdown()
    hub.close()
    assert hub.export_markdown() == first
