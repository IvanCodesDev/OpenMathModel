"""审批卡的「上一轮反馈」（H4）：回退重做轮里开出的闸门，把反馈包摘成拍板的人看得懂的几行。

反馈包（``TaskRunSnapshot.iteration_feedback``）由 reducer 在回退落地前打好：回退原因、谁触发的
（图的条件边 / 闸门上的重做选项 / 用户在对话或修订轮里的原话）、第几轮、被作废的上一轮产出。
重做的节点与审稿人早就在读它，拍板的人却看不到——重做轮的 G1 / G2 / G4 与首轮长得一样，G3 只在
标题里多一句「较上一轮」。闸门投影时把它摘成：

- 第一行是来由：从哪一段起回退重做、谁触发的、第几轮；
- 之后每行一条事实（「- 」开头）：回退原因、作废了哪几段的上一轮产出，再按闸门所在阶段取材——
  结果验证（G3）取上一轮未过的稳健性检查与审稿阻断意见，论文撰写（G4）取上一轮终稿审计发现与
  质量告警，建模方案（G1）取上一轮推荐的方案，数据准备（G2）取上一轮的数据结论。

结果存进审批行的 ``evidence["feedback"]``，经契约现成的 ``description`` 字段给到工作台视图与审批
接口。只转述事实、数字原样，不评判本轮好坏；首轮闸门没有反馈包，不出现。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from omm_agent_core import WORK_SEQUENCE
from omm_agent_skills import summarize_kinds

from .workflow import STAGE_LABELS

#: 逐条点名的上限：给拍板用的样本，不是搬运整份审稿 / 审计报告。
CHECKS_LIMIT = 4
BLOCKERS_LIMIT = 3
FINDINGS_LIMIT = 3
WARNINGS_LIMIT = 2
#: 单条事实里引用的原文（检查说明、审稿意见、审计发现）与回退原因的字数上限。
ITEM_CHARS = 160
REASON_CHARS = 200
#: 契约上限：工作台视图 ``pending_approval.description`` 2000 字、审批接口 ``description`` 4000 字。
VIEW_DESCRIPTION_CHARS = 2000
API_DESCRIPTION_CHARS = 4000

_REDO_OPTION = re.compile(r"^redo:[A-Z_]+$")
_AUTO_REASON_PREFIX = re.compile(r"^图 \S+ 条件边自动回退：")
_STATE_ORDER = [state.value for state in WORK_SEQUENCE]


def _label(state: Any) -> str:
    text = str(state or "")
    return STAGE_LABELS.get(text, text or "?")


def _clip(text: Any, limit: int = ITEM_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _round(feedback: Mapping[str, Any]) -> int | None:
    iteration = feedback.get("iteration")
    return iteration if isinstance(iteration, int) and not isinstance(iteration, bool) else None


def _more(total: int, shown: int, unit: str) -> str:
    return f"；另有 {total - shown} {unit}" if total > shown else ""


def _blockers(review: Any) -> list[Mapping[str, Any]]:
    return [
        entry
        for entry in _mappings(_mapping(review).get("findings"))
        if entry.get("severity") == "blocker"
    ]


def _blocker_text(entry: Mapping[str, Any]) -> str:
    location = _clip(entry.get("location"), 40)
    issue = _clip(entry.get("issue"))
    return f"{location}：{issue}" if location else issue


def _blocker_line(subject: str, blockers: list[Mapping[str, Any]]) -> str:
    shown = blockers[:BLOCKERS_LIMIT]
    return (
        f"上一轮{subject}未解决的阻断意见 {len(blockers)} 条："
        + "；".join(_blocker_text(entry) for entry in shown)
        + _more(len(blockers), len(shown), "条")
    )


def _check_text(check: Mapping[str, Any]) -> str:
    name = _clip(check.get("name") or check.get("id") or "检查", 40)
    numbers = [
        f"{label} {check.get(key)}"
        for key, label in (("value", "实测"), ("threshold", "阈值"))
        if check.get(key) is not None
    ]
    return f"{name}（{'，'.join(numbers)}）" if numbers else name


def _validation_facts(superseded: Mapping[str, Any]) -> list[str]:
    """G3：上一轮未过的稳健性检查（口径同 G3 闸门——检验脚本真跑通才算）与两条审稿环的阻断意见。"""
    lines: list[str] = []
    robustness = _mapping(_mapping(superseded.get("VALIDATING")).get("robustness"))
    failed = _mappings(robustness.get("failed_checks"))
    if robustness.get("executed") and robustness.get("status") == "passed" and failed:
        total = robustness.get("checks_total")
        shown = failed[:CHECKS_LIMIT]
        lines.append(
            f"上一轮稳健性检查{f' {total} 项中' if total else ''} {len(failed)} 项未通过："
            + "；".join(_check_text(check) for check in shown)
            + _more(len(failed), len(shown), "项")
        )
    experiment_blockers = _blockers(_mapping(superseded.get("EXPERIMENTING")).get("review"))
    if experiment_blockers:
        lines.append(_blocker_line("实验审稿", experiment_blockers))
    checks_blockers = _blockers(robustness.get("review"))
    if checks_blockers:
        lines.append(_blocker_line("检验脚本审稿", checks_blockers))
    return lines


def _finding_text(finding: Mapping[str, Any]) -> str:
    scope = _clip(finding.get("scope"), 40) or "（未标注位置）"
    tokens = [str(token) for token in finding.get("numbers") or [] if str(token).strip()][:3]
    detail = _clip(finding.get("detail"))
    return f"{scope}：{detail}" + (f"（涉及 {'、'.join(tokens)}）" if tokens else "")


def _paper_facts(superseded: Mapping[str, Any]) -> list[str]:
    """G4：上一轮草稿的终稿审计发现（分类计数 + 前几处）与质量告警。"""
    lines: list[str] = []
    paper = _mapping(superseded.get("PAPER_WRITING"))
    findings = _mappings(paper.get("audit_findings"))
    if findings:
        shown = findings[:FINDINGS_LIMIT]
        lines.append(
            f"上一轮终稿审计发现 {len(findings)} 处（{summarize_kinds(findings)}）："
            + "；".join(_finding_text(finding) for finding in shown)
            + _more(len(findings), len(shown), "处")
        )
    elif isinstance(paper.get("audit_findings"), list):
        lines.append("上一轮终稿审计没有发现问题")
    warnings = [_clip(item) for item in paper.get("quality_warnings") or [] if str(item).strip()]
    if warnings:
        lines.append("上一轮质量告警：" + "；".join(warnings[:WARNINGS_LIMIT]))
    return lines


def _planning_facts(superseded: Mapping[str, Any]) -> list[str]:
    """G1：上一轮推荐的方案（名称与做法），候选不止一个时给出数目。"""
    planning = _mapping(superseded.get("MODEL_PLANNING"))
    plans = _mappings(planning.get("plans"))
    if not plans:
        return []
    recommended = str(planning.get("recommended_plan_id") or "")
    plan = next((item for item in plans if str(item.get("id") or "") == recommended), plans[0])
    name = _clip(plan.get("name") or plan.get("id"), 60)
    approach = _clip(plan.get("approach"), 100)
    lines = [f"上一轮推荐的方案：{name}" + (f"——{approach}" if approach else "")]
    if len(plans) > 1:
        lines.append(f"上一轮共提出 {len(plans)} 个候选方案")
    return lines


def _data_facts(superseded: Mapping[str, Any]) -> list[str]:
    """G2：上一轮的数据画像结论与清洗结论。"""
    data = _mapping(superseded.get("DATA_PREPARATION"))
    lines: list[str] = []
    profile = _clip(data.get("profile_summary"), 120)
    if profile:
        lines.append(f"上一轮数据画像：{profile}")
    cleaning = _mapping(data.get("cleaning"))
    summary = _clip(cleaning.get("summary"), 120) if cleaning.get("executed") else ""
    if summary:
        lines.append(f"上一轮清洗结论：{summary}")
    return lines


_FACTS_BY_STATE: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "VALIDATING": _validation_facts,
    "PAPER_WRITING": _paper_facts,
    "MODEL_PLANNING": _planning_facts,
    "DATA_PREPARATION": _data_facts,
}


def _origin(feedback: Mapping[str, Any]) -> str:
    if feedback.get("auto"):
        return "图按条件边自动回退"
    if _REDO_OPTION.match(str(feedback.get("reason") or "")):
        return f"在「{_label(feedback.get('from_state'))}」的闸门上选了重做"
    return "按你的要求"


def _reason_line(feedback: Mapping[str, Any]) -> str | None:
    reason = str(feedback.get("reason") or "").strip()
    if not reason or _REDO_OPTION.match(reason):
        return None  # 闸门选项 id：来由一行已经说清了
    if feedback.get("auto"):
        return f"回退原因：{_clip(_AUTO_REASON_PREFIX.sub('', reason), REASON_CHARS)}"
    return f"你的要求：{_clip(reason, REASON_CHARS)}"


def feedback_digest(feedback: Any, gate_state: str) -> dict[str, Any] | None:
    """反馈包 → 审批行 evidence 里的 ``feedback``；没有反馈包或闸门在回退起点上游 → None。"""
    package = _mapping(feedback)
    target = str(package.get("target_state") or "")
    if target not in _STATE_ORDER:
        return None
    if gate_state in _STATE_ORDER and _STATE_ORDER.index(gate_state) < _STATE_ORDER.index(target):
        return None
    iteration = _round(package)
    lead = f"本轮是从「{_label(target)}」起的回退重做（{_origin(package)}" + (
        f"，第 {iteration} 轮）" if iteration else "）"
    )
    items: list[str] = []
    reason = _reason_line(package)
    if reason:
        items.append(reason)
    superseded = _mapping(package.get("superseded"))
    stages = [state for state in _STATE_ORDER if state in superseded]
    if stages:
        items.append("作废的上一轮产出：" + "、".join(_label(state) for state in stages))
    facts = _FACTS_BY_STATE.get(gate_state)
    if facts is not None:
        items.extend(facts(superseded))
    text = "\n".join([lead, *(f"- {item}" for item in items)])
    return {
        "target_state": target,
        "from_state": str(package.get("from_state") or "") or None,
        "auto": bool(package.get("auto")),
        "iteration": iteration,
        "text": _fit(text, API_DESCRIPTION_CHARS),
    }


def _fit(text: str, limit: int) -> str:
    """超长时在 ``limit`` 之内最后一个整行处截断，再标省略；一行都放不下才硬截。"""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 2)
    return (text[:cut] if cut > 0 else text[: limit - 2]) + "\n…"


def approval_description(evidence: Any, limit: int) -> str | None:
    """审批行 evidence → 契约 ``description``（「上一轮反馈」正文）；不是重做轮的闸门 → None。"""
    text = str(_mapping(_mapping(evidence).get("feedback")).get("text") or "").strip()
    return _fit(text, limit) if text else None


__all__ = (
    "API_DESCRIPTION_CHARS",
    "VIEW_DESCRIPTION_CHARS",
    "approval_description",
    "feedback_digest",
)
