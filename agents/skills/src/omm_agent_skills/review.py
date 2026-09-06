"""实验阶段「生成者-评审者」（§8.4）的纯函数件：复跑核对、终答归一化、材料拼接。

Reviewer 是实验节点在沙盒执行体验收通过之后派发的子代理（``kind="reviewer"``，
独立上下文、只读工具），一票驳回退 R2、僵持进 G3。本模块不依赖 ``nodes``：
这里只有可单测的纯函数与拍板常量，派发与驳回环在 ``nodes.ExperimentExecutionNode``。

两条纪律（与验证阶段的稳健性复跑同源）：**复跑核对是确定性的**（节点自己再跑一次
最终脚本、逐键比对指标），模型不得「读代码想象结果」；**驳回必须有 blocker**，
reject 而列不出一条 blocker 按 accept 记（意见照录），防止无理由驳回耗预算。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from omm_agent_harness import LoopBudget

from .chat_adapter import tool_protocol_note

__all__ = [
    "RERUN_ABS_TOL",
    "RERUN_REL_TOL",
    "REVIEWER_KNOWLEDGE_TOOL_NAMES",
    "REVIEWER_LOOP_BUDGET",
    "REVIEWER_MAX_TOOL_ROUNDS",
    "REVIEWER_TOOL_NAMES",
    "REVIEW_MAX_FINDINGS",
    "REVIEW_MAX_ROUNDS",
    "REVIEW_PROMPT_ID",
    "REVIEW_SEVERITIES",
    "compare_metrics",
    "findings_material",
    "normalize_verdict",
    "rerun_material",
    "review_feedback",
    "reviewer_tool_brief",
    "verdict_summary_text",
]

#: 审稿人角色卡模板 id（与 agents/prompts 文件名一致；五处白名单都要有它）。
REVIEW_PROMPT_ID = "experiment_review.default"

#: 审稿人的只读工作区工具（看产物表 / 数据文件是否真如脚本所言）；运行部分
#: 由节点确定性完成，子代理拿不到 python_run / ws_write（§8.2 落地口径：
#: 运行 = 节点、只读 = 子代理）。
REVIEWER_TOOL_NAMES: tuple[str, ...] = ("ws_read", "ws_list")
#: 知识库两个只读工具：装配注入了知识端口才列（方法的已知坑、同类赛题获奖做法）。
REVIEWER_KNOWLEDGE_TOOL_NAMES: tuple[str, ...] = ("knowledge_search", "knowledge_read")
#: 审稿会话至多几轮工具信封：读产物表 → 查一次知识库 → 顺链读一张卡。
REVIEWER_MAX_TOOL_ROUNDS = 3
#: 审稿内环预算（与提议人同款）：工具轮上限 + 一轮「已用完」观察 + 终答轮。
REVIEWER_LOOP_BUDGET = LoopBudget(
    max_turns=REVIEWER_MAX_TOOL_ROUNDS + 2, repairs=1, no_progress_k=2, tool_fail_m=2
)

#: 审稿轮数上限：首审 + 驳回修复后的复审一次；再驳回即僵持（§8.4「僵持到预算尽 → 上闸门」）。
REVIEW_MAX_ROUNDS = 2
#: 终答里最多保留几条意见（多余的截断，blocker 优先）。
REVIEW_MAX_FINDINGS = 8
REVIEW_SEVERITIES: tuple[str, ...] = ("blocker", "major", "minor")

#: 复跑指标比对容差：同种子应逐位一致，容差只吞并行归约 / BLAS 的浮点抖动。
RERUN_REL_TOL = 1e-6
RERUN_ABS_TOL = 1e-9

_SEVERITY_ALIASES = {
    "blocker": "blocker",
    "block": "blocker",
    "critical": "blocker",
    "fatal": "blocker",
    "high": "blocker",
    "阻断": "blocker",
    "严重": "blocker",
    "major": "major",
    "medium": "major",
    "重要": "major",
    "一般": "major",
    "minor": "minor",
    "low": "minor",
    "info": "minor",
    "轻微": "minor",
    "建议": "minor",
}

_VERDICT_ALIASES = {
    "accept": "accept",
    "accepted": "accept",
    "approve": "accept",
    "approved": "accept",
    "pass": "accept",
    "通过": "accept",
    "接受": "accept",
    "reject": "reject",
    "rejected": "reject",
    "fail": "reject",
    "驳回": "reject",
    "拒绝": "reject",
}


def reviewer_tool_brief(tools: Sequence[str]) -> str:
    """审稿会话的开场消息：工具协议（单一出处 chat_adapter）+ 审稿策略。"""
    has_knowledge = any(name in REVIEWER_KNOWLEDGE_TOOL_NAMES for name in tools)
    strategy = (
        "\n\n审稿策略：先对照方案、假设表与符号表静读脚本正文（实现是否偷换假设、"
        "指标口径是否与基线同口径、随机种子是否显式使用、结果表是否真的写出）；"
        "复跑核对结果已由系统给出，不要凭想象推断运行结果。需要看产物表或数据文件时"
        f"用 ws_read / ws_list，每次一个信封、全程至多 {REVIEWER_MAX_TOOL_ROUNDS} 次。"
    )
    if has_knowledge:
        strategy += (
            "对方法本身有疑问时可用 knowledge_search 查同类赛题的获奖做法与已知坑，"
            "命中后用 knowledge_read 读全卡再下结论；借鉴到的卡片在 issue 里标出处 id。"
        )
    strategy += (
        "只有确认存在会让结果不可信的问题才判 reject，且必须至少给出一条 blocker 并写清"
        "位置与修法；风格与可读性问题最多记 minor。不需要检索就直接输出终答。"
    )
    return (
        tool_protocol_note(tools, final_hint="按「输出要求」输出终答 JSON（终答不含 tool 键）")
        + strategy
    )


# ── 复跑核对 ────────────────────────────────────────────────────────────────


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numbers_close(recorded: float, rerun: float) -> bool:
    if math.isnan(recorded) and math.isnan(rerun):
        return True
    return math.isclose(recorded, rerun, rel_tol=RERUN_REL_TOL, abs_tol=RERUN_ABS_TOL)


def compare_metrics(
    recorded: Mapping[str, Any], rerun: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """首跑指标 vs 复跑指标：逐键比对，返回 (一致, 差异行)。

    数值键按容差比较，其它类型逐字节；两边键集不同也算差异。空指标（复跑没
    打印标记行）不算一致——复跑连指标都没打出来就是不可复现。
    """
    diffs: list[str] = []
    if not rerun:
        return False, ["复跑未打印 OMM_METRICS_JSON 标记行"]
    for key in sorted(set(recorded) | set(rerun)):
        if key not in rerun:
            diffs.append(f"{key}：首跑 {recorded[key]!r}，复跑缺失")
            continue
        if key not in recorded:
            diffs.append(f"{key}：首跑缺失，复跑 {rerun[key]!r}")
            continue
        before, after = recorded[key], rerun[key]
        if _is_number(before) and _is_number(after):
            if not _numbers_close(float(before), float(after)):
                diffs.append(f"{key}：首跑 {before!r}，复跑 {after!r}")
        elif before != after or type(before) is not type(after):
            # bool 是 int 子类（True == 1）：类型也要一样才算同一个值
            diffs.append(f"{key}：首跑 {before!r}，复跑 {after!r}")
    return not diffs, diffs


def rerun_material(rerun: Mapping[str, Any]) -> str:
    """复跑核对结果 → 审稿任务卡的一段文字（确定性事实，模型只能引用不能改写）。"""
    if not rerun.get("executed"):
        return "未复跑：" + str(rerun.get("reason") or "无原因说明")
    if rerun.get("consistent"):
        return "已用同一份脚本与同一随机种子复跑一次：退出码 0，核心指标与首跑逐键一致。"
    lines = ["已复跑一次，但结果**与首跑不一致**（可复现性存疑）："]
    if rerun.get("reason"):
        lines.append(f"- {rerun['reason']}")
    lines.extend(f"- {diff}" for diff in rerun.get("diff") or [])
    return "\n".join(lines)


# ── 终答归一化 ──────────────────────────────────────────────────────────────


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_findings(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            continue
        issue = _text(item.get("issue") or item.get("description") or item.get("text"))
        if not issue:
            continue
        severity = _SEVERITY_ALIASES.get(_text(item.get("severity")).lower(), "minor")
        findings.append({
            "id": _text(item.get("id")) or f"R{index}",
            "severity": severity,
            "location": _text(item.get("location")),
            "issue": issue,
            "fix_hint": _text(item.get("fix_hint") or item.get("fix") or item.get("suggestion")),
        })
    # blocker 在前、同级保序；超出上限的从末尾截掉
    order = {name: rank for rank, name in enumerate(REVIEW_SEVERITIES)}
    findings.sort(key=lambda entry: order[entry["severity"]])
    return findings[:REVIEW_MAX_FINDINGS]


def normalize_verdict(output: Mapping[str, Any]) -> dict[str, Any]:
    """审稿终答 → 节点口径：``{"verdict", "findings", "blockers", "summary"}``。

    一票驳回的成立条件是 ``reject`` **且** 至少一条 blocker；reject 而无 blocker
    按 accept 记（意见照录进 findings），未知 verdict 词也按 accept——审稿人只有
    说得出「哪里会让结果不可信」才拿得到否决权。
    """
    findings = _normalize_findings(output.get("findings"))
    blockers = [entry for entry in findings if entry["severity"] == "blocker"]
    verdict = _VERDICT_ALIASES.get(_text(output.get("verdict")).lower(), "accept")
    if verdict == "reject" and not blockers:
        verdict = "accept"
    return {
        "verdict": verdict,
        "findings": findings,
        "blockers": len(blockers),
        "summary": _text(output.get("summary")),
    }


# ── 材料拼接 ────────────────────────────────────────────────────────────────


def findings_material(findings: Sequence[Mapping[str, Any]]) -> str:
    """意见列表 → 每行「[id｜severity] location：issue（修法）」；空表为「无」。"""
    lines = []
    for entry in findings:
        head = f"[{entry.get('id') or '-'}｜{entry.get('severity') or 'minor'}]"
        location = _text(entry.get("location"))
        issue = _text(entry.get("issue"))
        hint = _text(entry.get("fix_hint"))
        line = f"{head} " + (f"{location}：" if location else "") + issue
        if hint:
            line += f"（修法：{hint}）"
        lines.append(line)
    return "\n".join(lines) if lines else "无"


def review_feedback(
    findings: Sequence[Mapping[str, Any]], summary: str, rerun: Mapping[str, Any]
) -> str:
    """驳回意见 → 修复波任务说明的追加段（生成者必须逐条处理，不得只改叙述）。"""
    parts = [
        "## 审稿驳回意见（独立审稿人核查结论；必须逐条处理并重新运行，不得只改文字说明）",
        findings_material(findings),
    ]
    if summary:
        parts.append(f"审稿总结：{summary}")
    if rerun.get("executed") and not rerun.get("consistent"):
        parts.append("复跑核对：" + rerun_material(rerun))
    return "\n\n".join(parts)


def verdict_summary_text(review: Mapping[str, Any]) -> str:
    """面向用户的一句话审稿结论（进度旁路 / 进度叙述用）。"""
    if not review.get("executed"):
        return "未经独立审稿：" + str(review.get("reason") or "")
    rounds = int(review.get("rounds") or 0)
    blockers = int(review.get("blockers") or 0)
    if review.get("stalemate"):
        return (
            f"独立审稿 {rounds} 轮后仍有 {blockers} 条阻断性意见未解决"
            f"（{review.get('reason') or '僵持'}），交结果采用闸门裁定"
        )
    if review.get("verdict") == "accept":
        count = len(review.get("findings") or [])
        return f"独立审稿通过（{rounds} 轮，{count} 条意见）"
    return json.dumps(dict(review), ensure_ascii=False)
