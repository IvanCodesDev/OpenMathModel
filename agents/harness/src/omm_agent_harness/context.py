"""ContextAssembler (design §4.2): sectioned, budgeted, deterministic prompts.

Standard section order: System(角色卡+纪律) → TaskFrame(题面/子问题切片) →
Memory(前序 StageOutput 摘要) → Evidence(工件摘要+id) → Tools(schema) →
OutputSpec(Schema+示例). Callers pass sections in the order they want; the
constant below documents the standard for stage skills.

Determinism contract (§6.5 point 3): assembly is a PURE FUNCTION — same
sections in, same messages and same ``prompt_hash`` out. No clock, no ids, no
randomness. The hash goes into the ``llm.chat`` audit event so evals can
assert "same input, same prompt" without comparing full text.

Budget semantics: ``max_chars`` is a per-section cap (character proxy for
tokens — honest and cheap; real tokenizer counting is a later refinement).
Overflow follows the section's declared policy: tail/head truncation with an
explicit marker, or fail-fast for sections that must never be cut (assembly
defect, §4.9 spirit). Raw attachments never enter prompts whole — that rule
lives with callers, who pass summaries+ids as Evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from .gateway import Message

__all__ = [
    "AssembledPrompt",
    "AssemblyError",
    "ContextAssembler",
    "STANDARD_SECTION_ORDER",
    "Section",
]

#: The §4.2 standard order for stage skills (callers may deviate knowingly).
STANDARD_SECTION_ORDER = (
    "system",
    "task_frame",
    "memory",
    "evidence",
    "tools",
    "output_spec",
)

_OVERFLOW_POLICIES = frozenset({"truncate_tail", "truncate_head", "fail"})


class AssemblyError(ValueError):
    """Assembly-time defect: bad policy or an overflowing must-not-cut section."""


@dataclass(frozen=True)
class Section:
    """One prompt section with its own budget and overflow policy."""

    name: str
    content: str
    max_chars: int | None = None
    overflow: str = "truncate_tail"
    heading: str | None = None  # rendered as "## heading" in the user message

    def __post_init__(self) -> None:
        if self.overflow not in _OVERFLOW_POLICIES:
            raise AssemblyError(
                f"section {self.name!r}: unknown overflow policy {self.overflow!r}"
            )
        if self.max_chars is not None and self.max_chars <= 0:
            raise AssemblyError(f"section {self.name!r}: max_chars must be positive")


@dataclass(frozen=True)
class AssembledPrompt:
    """Deterministic assembly result; hash is the audit anchor."""

    messages: tuple[Message, ...]
    prompt_hash: str
    truncated_sections: tuple[str, ...]

    @property
    def total_chars(self) -> int:
        return sum(len(message.content) for message in self.messages)


def _fit(section: Section) -> tuple[str, bool]:
    """Apply the section budget; returns (content, was_truncated)."""
    text = section.content
    limit = section.max_chars
    if limit is None or len(text) <= limit:
        return text, False
    if section.overflow == "fail":
        raise AssemblyError(
            f"section {section.name!r} is {len(text)} chars, over its "
            f"max_chars={limit} and declared overflow='fail'"
        )
    dropped = len(text) - limit
    if section.overflow == "truncate_head":
        return f"…（前段已截断 {dropped} 字符）" + text[-limit:], True
    return text[:limit] + f"…（已截断 {dropped} 字符）", True


class ContextAssembler:
    """Pure-function prompt assembly. No instance state by design."""

    @staticmethod
    def build(sections: Sequence[Section]) -> AssembledPrompt:
        system_parts: list[str] = []
        user_parts: list[str] = []
        truncated: list[str] = []

        for section in sections:
            if not section.content.strip():
                continue  # empty sections vanish, they carry no information
            content, was_truncated = _fit(section)
            if was_truncated:
                truncated.append(section.name)
            if section.name == "system":
                system_parts.append(content)
            else:
                if section.heading:
                    user_parts.append(f"## {section.heading}\n{content}")
                else:
                    user_parts.append(content)

        messages: list[Message] = []
        if system_parts:
            messages.append(Message(role="system", content="\n\n".join(system_parts)))
        if user_parts:
            messages.append(Message(role="user", content="\n\n".join(user_parts)))

        canonical = json.dumps(
            [[message.role, message.content] for message in messages],
            ensure_ascii=False,
        )
        prompt_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return AssembledPrompt(
            messages=tuple(messages),
            prompt_hash=prompt_hash,
            truncated_sections=tuple(truncated),
        )
