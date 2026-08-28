"""E1 tests for ContextAssembler: pure-function determinism and budgets (§4.2)."""

from __future__ import annotations

import pytest

from omm_agent_harness import AssemblyError, ContextAssembler, Section


def sections_fixture() -> list[Section]:
    return [
        Section(name="system", content="你是数据分析技能，遵守输出纪律。"),
        Section(name="task_frame", content="题面：预测季度运量。", heading="任务"),
        Section(name="memory", content="上一阶段结论：数据无缺失。", heading="前序结论"),
        Section(name="output_spec", content='输出 JSON：{"answer": number}', heading="输出要求"),
    ]


def test_same_input_same_hash_and_messages() -> None:
    first = ContextAssembler.build(sections_fixture())
    second = ContextAssembler.build(sections_fixture())
    assert first.prompt_hash == second.prompt_hash
    assert first.messages == second.messages


def test_hash_changes_when_any_content_changes() -> None:
    base = ContextAssembler.build(sections_fixture())
    changed_sections = sections_fixture()
    changed_sections[1] = Section(
        name="task_frame", content="题面：预测年度运量。", heading="任务"
    )
    changed = ContextAssembler.build(changed_sections)
    assert base.prompt_hash != changed.prompt_hash


def test_system_section_becomes_system_message_rest_merge_into_user() -> None:
    prompt = ContextAssembler.build(sections_fixture())
    assert [message.role for message in prompt.messages] == ["system", "user"]
    user = prompt.messages[1].content
    # Section order and headings are preserved in the user message.
    assert user.index("## 任务") < user.index("## 前序结论") < user.index("## 输出要求")


def test_empty_sections_are_skipped() -> None:
    prompt = ContextAssembler.build(
        [
            Section(name="system", content="   "),
            Section(name="task_frame", content="正文"),
        ]
    )
    assert [message.role for message in prompt.messages] == ["user"]


def test_truncate_tail_applies_budget_and_records_section() -> None:
    prompt = ContextAssembler.build(
        [Section(name="evidence", content="A" * 500, max_chars=100)]
    )
    body = prompt.messages[0].content
    assert body.startswith("A" * 100) and "已截断 400 字符" in body
    assert prompt.truncated_sections == ("evidence",)


def test_truncate_head_keeps_tail() -> None:
    content = "HEAD" + "x" * 500 + "TAIL"
    prompt = ContextAssembler.build(
        [Section(name="memory", content=content, max_chars=50, overflow="truncate_head")]
    )
    body = prompt.messages[0].content
    assert body.endswith("TAIL") and "前段已截断" in body


def test_overflow_fail_raises_assembly_error() -> None:
    with pytest.raises(AssemblyError):
        ContextAssembler.build(
            [Section(name="output_spec", content="x" * 10, max_chars=5, overflow="fail")]
        )


def test_unknown_overflow_policy_rejected_at_construction() -> None:
    with pytest.raises(AssemblyError):
        Section(name="memory", content="x", overflow="explode")


def test_within_budget_content_untouched() -> None:
    prompt = ContextAssembler.build(
        [Section(name="task_frame", content="short", max_chars=100)]
    )
    assert prompt.messages[0].content == "short"
    assert prompt.truncated_sections == ()
