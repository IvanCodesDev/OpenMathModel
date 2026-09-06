"""六阶段真实节点的最新成功输出 → 五类页面正文投影。

数据源是 ``run_domain_events``（执行事实来源，见 engine_glue.py 顶部说明）：
按 seq 顺序重放 STEP_STARTED/STEP_SUCCEEDED 事件，记录每个 step_id 归属的节点，
再把 STEP_SUCCEEDED 的 ``payload.outputs`` 按节点覆盖式收敛为「该节点最新一次
成功输出」——重试/退回重做时旧尝试的输出自然被新尝试覆盖。

模拟链路（sim-0.1，未配置自定义 API）的节点只产出 ``{"label": ...}``，不含
契约要求的字段：对应投影整体为 null（与各契约描述一致——「模拟链或阶段未
完成时该投影整体为 null」）。判定依据是各阶段节点 output_schema 的必填键
（profile_summary / plans / approach_summary / verdict / title），空值兜底
对象会违反契约硬约束（plans 的 minItems=1、verdict 的 enum）把接口打成 500。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_contracts import (
    ApprovalDecisionType,
    ApprovalStatus,
    DatasetProfile,
    DeliveryManifest,
    DocumentDraft,
    ExperimentSummary,
    PlanProposal,
    ProblemFrame,
)

from .api_models import StageOutputs
from .blobstore import ArtifactBlobStore, has_readable_local_content
from .orm import (
    ApprovalRequestRow,
    ArtifactRow,
    DomainEventRow,
    StageOutputRow,
    StepRunRow,
    TaskRunRow,
)
from .serialize import as_utc, iso_z


def superseded_step_ids(steps: Iterable[StepRunRow]) -> set[str]:
    """同一节点多趟 step 里，除最近一趟（attempt 最大）之外的 step id。

    修订回合重做（ADR-0013）与失败重试都会让同一节点出现多趟；成果页与交付
    清单只列最近一趟产出的文件——审批门承诺的正是「原有成果由本轮新结果替换」，
    两轮产物并列（重名两两、数量翻倍）会让用户分不清哪份是现行的。旧趟的产物
    行与内容对象都还在库里（可审计、可经 /artifacts/{id}/download 直接取），
    只是不再列出。上传附件没有 producer_step，不受影响。
    """
    latest: dict[str, tuple[tuple[int, Any, str], str]] = {}
    seen: set[str] = set()
    for step in steps:
        seen.add(step.id)
        rank = (int(step.attempt or 0), step.created_at, step.id)
        current = latest.get(step.node)
        if current is None or rank > current[0]:
            latest[step.node] = (rank, step.id)
    return seen - {step_id for _, step_id in latest.values()}


def current_pass_artifacts(
    artifact_rows: Iterable[ArtifactRow], steps: Iterable[StepRunRow]
) -> list[ArtifactRow]:
    """过滤掉被后一趟替换的产物（见 :func:`superseded_step_ids`）。"""
    superseded = superseded_step_ids(steps)
    return [row for row in artifact_rows if (row.producer_step or "") not in superseded]

_PROBLEM_ANALYSIS = "PROBLEM_ANALYSIS"
_DATA_PREPARATION = "DATA_PREPARATION"
_MODEL_PLANNING = "MODEL_PLANNING"
_EXPERIMENTING = "EXPERIMENTING"
_VALIDATING = "VALIDATING"
_PAPER_WRITING = "PAPER_WRITING"

#: 契约 verdict 枚举（experiment-summary / delivery-manifest 共用 $defs）。
_VERDICTS = frozenset({"pass", "concerns", "fail"})

#: 各节点「有契约实质内容」的判定键（读侧空投影与写侧落行共用同一门槛，
#: 见各节点 prompt output_schema 的必填键）：模拟节点只有 {"label"} 不落行。
REQUIRED_OUTPUT_KEYS: dict[str, str] = {
    _PROBLEM_ANALYSIS: "title",
    _DATA_PREPARATION: "profile_summary",
    _MODEL_PLANNING: "plans",
    _EXPERIMENTING: "approach_summary",
    _VALIDATING: "verdict",
    _PAPER_WRITING: "title",
}

#: stage_outputs 表的 schema_id：标注 content 是「该节点 outputs 原文」的形状
#: 版本；六类页面正文契约（dataset-profile.v1 等）由读侧投影组装，不在此处。
STAGE_OUTPUT_SCHEMA_IDS: dict[str, str] = {
    node: f"{node.lower().replace('_', '-')}.outputs.v1" for node in REQUIRED_OUTPUT_KEYS
}


class StageState:
    """一个节点的「最新成功输出」：outputs 随后续 STEP_SUCCEEDED 覆盖，count 计成功次数。

    step_id 是产出这一版的那一趟 step：审批请求的 ``evidence.requested_by_step``
    指向同一个 id，决策台账据此只认「对这一版方案」的确认（退回重做后是新趟、
    新审批，旧决策自然不再套用）。

    公开给 workspace_view 复用（执行计划的本题化文案同样以阶段最新输出为数据源）。
    """

    __slots__ = ("outputs", "at", "count", "step_id")

    def __init__(self) -> None:
        self.outputs: dict[str, Any] = {}
        self.at: Optional[datetime] = None
        self.count = 0
        self.step_id: Optional[str] = None


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def replay_stage_outputs(
    session: Session, run_id: str
) -> tuple[dict[str, StageState], dict[str, str]]:
    """重放领域事件：(节点 → 最新成功输出状态, step_id → 归属节点)。"""
    rows = session.execute(
        select(DomainEventRow)
        .where(DomainEventRow.run_id == run_id)
        .order_by(DomainEventRow.seq.asc())
    ).scalars()

    step_nodes: dict[str, str] = {}
    stages: dict[str, StageState] = {}
    for row in rows:
        payload = row.payload or {}
        if row.event_type == "STEP_STARTED":
            step_id = payload.get("step_id")
            node = payload.get("state")
            if step_id is not None and node is not None:
                step_nodes[str(step_id)] = str(node)
            continue
        if row.event_type == "STEP_SUCCEEDED":
            step_id = str(payload.get("step_id"))
            node = step_nodes.get(step_id)
            if node is None:
                continue
            state = stages.setdefault(node, StageState())
            state.outputs = dict(payload.get("outputs") or {})
            state.at = _dt(row.created_at)
            state.count += 1
            state.step_id = step_id
    return stages, step_nodes


def overlay_stage_output_rows(
    session: Session, run_id: str, stages: dict[str, StageState]
) -> None:
    """stage_outputs 表的 current 行覆盖事件重放结果（表是版本化的持久事实）。

    旧运行没有行（表晚于运行上线）→ 重放结果原样保留；新运行两者一致，
    表行额外带上跨重试的版本号（count=version，superseded 历史可审计）。
    """
    rows = session.execute(
        select(StageOutputRow).where(
            StageOutputRow.run_id == run_id,
            StageOutputRow.status == "current",
        )
    ).scalars()
    for row in rows:
        state = stages.setdefault(row.node, StageState())
        state.outputs = dict(row.content or {})
        state.at = as_utc(row.created_at)
        state.count = max(state.count, int(row.version))
        if row.producer_step_id:
            state.step_id = str(row.producer_step_id)


def _strs(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


def _problem_frame(run_id: str, state: Optional[StageState]) -> Optional[ProblemFrame]:
    """读题正文投影（problem-frame.v1，H1）。

    门槛=objectives 在场（prompt v5 起 required；只有 title 的远古输出投影为
    null）。subquestions 是 v6 新增字段——旧运行未产出时如实给空列表（契约
    required 但允许空，消费方以空列表理解「未分解」）。
    """
    if state is None:
        return None
    outputs = state.outputs
    if "title" not in outputs or "objectives" not in outputs:
        return None
    subquestions = []
    for entry in outputs.get("subquestions") or []:
        if not isinstance(entry, dict):
            continue
        subquestions.append(
            {
                "id": str(entry.get("id") or ""),
                "text": str(entry.get("text") or ""),
                "depends_on": _strs(entry.get("depends_on")),
            }
        )
    return ProblemFrame(
        run_id=run_id,
        title=str(outputs.get("title") or ""),
        problem_type=str(outputs.get("problem_type") or ""),
        objectives=_strs(outputs.get("objectives")),
        constraints=_strs(outputs.get("constraints")),
        data_requirements=_strs(outputs.get("data_requirements")),
        key_assumptions=_strs(outputs.get("key_assumptions")),
        subquestions=subquestions,
        updated_at=iso_z(state.at),
    )


_CLEANING_STATUSES = frozenset({"passed", "failed"})


def _non_negative_int(value: Any) -> int:
    """标记行数字 → 契约的非负整数；bool / 负数 / 非数值一律归 0（不编数字）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _cleaning_report(raw: Any) -> Optional[dict[str, Any]]:
    """数据节点的 ``cleaning`` 输出 → 契约 ``cleaning_report``（dataset-profile.v1）。

    节点写的是过程全貌（llm_calls / target_columns / 产物引用 / 波次…），契约只要
    结论：执行与否及原因、最终采用波的验收结论、影响面四个数字两张列表、清洗工程师
    自述、独立审稿结论。白名单投影——契约 ``additionalProperties=false``，任何未
    清洗的键都会把接口打成 500。

    - 未执行（工具 / 监督者 / 会话端口缺席、无数据文件、预算不足、子代理未完成）：
      status null、数字 0、列表空、summary 空、review null，reason 原样保留；
    - 执行：数字按标记行取非负整数，删行比例缺失时按前后行数重算并夹到 [0, 1]；
      status 越界归 failed；review 走 ``_review_report``；
    - 该字段出现之前的运行 / 模拟节点没有该键 → None（契约 null）。
    """
    if not isinstance(raw, dict):
        return None
    executed = raw.get("executed") is True
    if not executed:
        return {
            "executed": False,
            "status": None,
            "reason": str(raw.get("reason") or ""),
            "attempts": 0,
            "rows_before": 0,
            "rows_after": 0,
            "rows_deleted_ratio": 0.0,
            "imputed_columns": [],
            "imputed_target_columns": [],
            "summary": "",
            "review": None,
        }
    rows_before = _non_negative_int(raw.get("rows_before"))
    rows_after = _non_negative_int(raw.get("rows_after"))
    ratio = raw.get("rows_deleted_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        ratio = (1.0 - rows_after / rows_before) if rows_before > 0 else 0.0
    status = str(raw.get("status") or "").strip().lower()
    return {
        "executed": True,
        "status": status if status in _CLEANING_STATUSES else "failed",
        "reason": "",
        "attempts": _non_negative_int(raw.get("attempts")),
        "rows_before": rows_before,
        "rows_after": rows_after,
        "rows_deleted_ratio": round(min(1.0, max(0.0, float(ratio))), 4),
        "imputed_columns": [c for c in _strs(raw.get("imputed_columns")) if c.strip()],
        "imputed_target_columns": [
            c for c in _strs(raw.get("imputed_target_columns")) if c.strip()
        ],
        "summary": str(raw.get("summary") or ""),
        "review": _review_report(raw.get("review")),
    }


def _dataset_profile(run_id: str, state: Optional[StageState]) -> Optional[DatasetProfile]:
    if state is None:
        return None
    outputs = state.outputs
    if "profile_summary" not in outputs:
        # sim 节点/旧运行：没有契约实质字段，该阶段视为「尚无可用正文」
        return None
    datasets = []
    for entry in outputs.get("datasets") or []:
        if not isinstance(entry, dict):
            continue
        datasets.append(
            {
                "name": str(entry.get("name") or ""),
                "source": str(entry.get("source") or ""),
                "fields": _strs(entry.get("fields")),
                "quality_risks": _strs(entry.get("quality_risks")),
            }
        )
    return DatasetProfile(
        run_id=run_id,
        profile_summary=str(outputs.get("profile_summary") or ""),
        datasets=datasets,
        preparation_steps=_strs(outputs.get("preparation_steps")),
        missing_value_strategy=outputs.get("missing_value_strategy"),
        outlier_strategy=outputs.get("outlier_strategy"),
        derived_features=_strs(outputs.get("derived_features")),
        # 清洗执行结论 + 独立审稿（§8.4 第三个沙盒消费方）；该字段出现之前的运行 → null
        cleaning=_cleaning_report(outputs.get("cleaning")),
        updated_at=iso_z(state.at),
    )


def _plan_proposal(
    run_id: str,
    state: Optional[StageState],
    approvals: Iterable[ApprovalRequestRow] = (),
) -> Optional[PlanProposal]:
    if state is None:
        return None
    outputs = state.outputs
    plans = []
    for entry in outputs.get("plans") or []:
        if not isinstance(entry, dict):
            continue
        plans.append(
            {
                "id": str(entry.get("id") or ""),
                "name": str(entry.get("name") or ""),
                "approach": str(entry.get("approach") or ""),
                "steps": _strs(entry.get("steps")),
                "risks": _strs(entry.get("risks")),
                "language": _language_or_none(entry.get("language")),
            }
        )
    if not plans:
        # sim 节点或退化输出：契约要求 plans 至少一项（minItems=1），空表投影为 null
        return None
    plan_ids = {plan["id"] for plan in plans if plan["id"]}
    recommended_plan_id = str(outputs.get("recommended_plan_id") or "")
    return PlanProposal(
        run_id=run_id,
        plans=plans,
        recommended_plan_id=recommended_plan_id,
        rationale=outputs.get("rationale"),
        assumptions=_assumptions(outputs.get("assumptions"), plan_ids),
        symbols=_symbols(outputs.get("symbols"), plan_ids),
        decision=_plan_decision(
            state, approvals, [plan["id"] for plan in plans], recommended_plan_id
        ),
        updated_at=iso_z(state.at),
    )


def _language_or_none(value: Any) -> Optional[str]:
    """方案卡实现语言：节点已归一到小写标识；旧运行没有该键 → null（消费者按 python 理解）。"""
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


#: G1 决策台账里算「正向确认」的选项：采用推荐案 / 改用某备选案。拒绝导致重做，
#: 不是对这一版方案的决策（与 omm_agent_skills.nodes 的 G1 选项 id 同一口径）。
_G1_DECISION_TYPE = ApprovalDecisionType.confirm_plan.value
_G1_RESOLVED = ApprovalStatus.RESOLVED.value
_G1_APPROVE_OPTION_ID = "approve"
_G1_ADOPT_OPTION_PREFIX = "adopt:"


def _plan_decision(
    state: StageState,
    approvals: Iterable[ApprovalRequestRow],
    plan_ids: list[str],
    recommended_plan_id: str,
) -> Optional[dict[str, Any]]:
    """这一版方案的 G1 决策（plan-proposal.v1 ``decision``）。

    只认 ``confirm_plan`` 且已解决、``evidence.requested_by_step`` 指向产出这一版
    的那一趟 step 的审批——同一趟不会挂两次审批，多条（理论上）取最晚解决的。
    ``chosen_plan_id`` 复现 ``chosen_plan`` 的选案规则（adopt 目标在表内 → 它；
    否则推荐案；否则首案），前端与下游节点因此看到同一个方案。
    """
    if not state.step_id or not plan_ids:
        return None
    candidates: list[tuple[str, ApprovalRequestRow, dict[str, Any]]] = []
    for approval in approvals:
        if approval.decision_type != _G1_DECISION_TYPE or approval.status != _G1_RESOLVED:
            continue
        evidence = approval.evidence if isinstance(approval.evidence, dict) else {}
        if str(evidence.get("requested_by_step") or "") != state.step_id:
            continue
        resolution = approval.resolution if isinstance(approval.resolution, dict) else {}
        option_id = str(resolution.get("option_id") or "")
        if option_id != _G1_APPROVE_OPTION_ID and not option_id.startswith(_G1_ADOPT_OPTION_PREFIX):
            continue
        candidates.append((str(resolution.get("resolved_at") or ""), approval, resolution))
    if not candidates:
        return None
    resolved_at, approval, resolution = max(candidates, key=lambda item: item[0])
    option_id = str(resolution["option_id"])
    chosen = option_id[len(_G1_ADOPT_OPTION_PREFIX):] if option_id.startswith(_G1_ADOPT_OPTION_PREFIX) else ""
    if chosen not in plan_ids:
        chosen = recommended_plan_id if recommended_plan_id in plan_ids else plan_ids[0]
    comment = resolution.get("comment")
    return {
        "approval_id": approval.id,
        "option_id": option_id,
        "chosen_plan_id": chosen,
        "actor": str(resolution.get("actor") or "user"),
        "comment": comment.strip() if isinstance(comment, str) and comment.strip() else None,
        "resolved_at": resolved_at or iso_z(as_utc(approval.requested_at)),
    }


#: 假设表 / 符号表的契约枚举（plan-proposal.v1 $defs.assumption / $defs.symbol）。
_ASSUMPTION_IMPACTS = frozenset({"low", "medium", "high"})
_ASSUMPTION_STATUSES = frozenset({"confirmed", "to_verify", "critical"})
_SYMBOL_KINDS = frozenset({"set", "parameter", "variable", "objective", "other"})
_GLOBAL_SCOPE = "global"


def _text_or_none(value: Any) -> Optional[str]:
    """可空文本列（unit / range）：非字符串或空白 → null。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _assumptions(raw: Any, plan_ids: set[str]) -> Optional[list[dict[str, Any]]]:
    """节点 outputs.assumptions → 契约 assumption[]（H3 切片 2）。

    键不存在（切片 2 之前的运行、无监督者的单次调用路径）或规范化失败写下的 null
    → null；畸形条目（缺 id / text、枚举值不在契约内）逐条剔除——契约
    additionalProperties=false 且枚举硬约束，透传等于让一条脏数据把整个接口打成
    500。scope 不是 "global" 也不是现有方案 id 时归为全局：方案被归约改名后，
    假设本身仍是有效信息，不因归属对不上而丢。
    """
    if not isinstance(raw, list):
        return None
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        impact = item.get("impact")
        status = item.get("status")
        if not entry_id or not text:
            continue
        if impact not in _ASSUMPTION_IMPACTS or status not in _ASSUMPTION_STATUSES:
            continue
        scope = str(item.get("scope") or "").strip()
        if scope not in plan_ids:
            scope = _GLOBAL_SCOPE
        entries.append(
            {
                "id": entry_id,
                "text": text,
                "scope": scope,
                "basis": str(item.get("basis") or ""),
                "impact": impact,
                "status": status,
            }
        )
    return entries


def _symbols(raw: Any, plan_ids: set[str]) -> Optional[list[dict[str, Any]]]:
    """节点 outputs.symbols → 契约 symbol[]（同上：缺省 null、畸形剔除）。

    plan_id 对不上现有方案时归为共享（null）；符号两侧若残留 ``$`` 定界一并剥掉，
    前端按行内公式统一包一层。
    """
    if not isinstance(raw, list):
        return None
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().strip("$").strip()
        definition = str(item.get("definition") or "").strip()
        kind = item.get("kind")
        if not symbol or not definition or kind not in _SYMBOL_KINDS:
            continue
        plan_id = item.get("plan_id")
        plan_id = str(plan_id).strip() if isinstance(plan_id, str) and plan_id.strip() in plan_ids else None
        entries.append(
            {
                "symbol": symbol,
                "kind": kind,
                "definition": definition,
                "unit": _text_or_none(item.get("unit")),
                "range": _text_or_none(item.get("range")),
                "plan_id": plan_id,
            }
        )
    return entries


def _number_or_none(value: Any) -> Optional[float]:
    """标记行里的数值；bool 是 int 的子类但不算数字，其它非数值一律 null。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _threshold(value: Any) -> Any:
    """阈值允许数值或脚本给出的文字口径（如「≤ 0.05」）；空串 / 其它类型 → null。"""
    number = _number_or_none(value)
    if number is not None:
        return number
    if isinstance(value, str) and value.strip():
        return value
    return None


_REVIEW_VERDICTS = frozenset({"accept", "reject"})
_REVIEW_SEVERITIES = frozenset({"blocker", "major", "minor"})


def _review_report(raw: Any) -> Optional[dict[str, Any]]:
    """生成者-评审者环的 ``review`` 输出 → 契约 ``review_report``（experiment-summary.v1）。

    实验节点 ``outputs["review"]`` 与验证节点 ``robustness["review"]`` 同一形状
    （nodes._run_review_loop）：未执行 ``{executed: false, reason, llm_calls[, rounds,
    rerun]}``；已执行还带 verdict / findings / blockers / summary / rerun / stalemate。
    契约九键全必填且 additionalProperties=false：未执行形状补齐空值；过程字段
    （llm_calls、rerun 的 metrics / diff）剔除；意见逐条清洗（无 issue 剔除、
    severity 枚举外按 minor、缺 id 按序补）；``blockers`` 按投影后的意见重算。
    审稿环之前的运行与模拟节点没有该键 → null（契约允许）。
    """
    if not isinstance(raw, dict):
        return None
    executed = raw.get("executed") is True
    findings: list[dict[str, Any]] = []
    if executed:
        for index, entry in enumerate(raw.get("findings") or [], start=1):
            if not isinstance(entry, dict):
                continue
            issue = str(entry.get("issue") or "").strip()
            if not issue:
                continue
            severity = str(entry.get("severity") or "").strip().lower()
            findings.append(
                {
                    "id": str(entry.get("id") or "").strip() or f"R{index}",
                    "severity": severity if severity in _REVIEW_SEVERITIES else "minor",
                    "location": str(entry.get("location") or "").strip(),
                    "issue": issue,
                    "fix_hint": str(entry.get("fix_hint") or "").strip(),
                }
            )
    verdict = raw.get("verdict") if executed else None
    rounds = raw.get("rounds")
    rerun = raw.get("rerun")
    rerun_consistent: Optional[bool] = None
    if isinstance(rerun, dict) and rerun.get("executed") is True:
        rerun_consistent = rerun.get("consistent") is True
    return {
        "executed": executed,
        "verdict": verdict if verdict in _REVIEW_VERDICTS else None,
        "rounds": rounds if isinstance(rounds, int) and not isinstance(rounds, bool) and rounds >= 0 else 0,
        "findings": findings,
        "blockers": sum(1 for finding in findings if finding["severity"] == "blocker"),
        "summary": str(raw.get("summary") or "") if executed else "",
        "stalemate": raw.get("stalemate") is True,
        "rerun_consistent": rerun_consistent,
        "reason": str(raw.get("reason") or ""),
    }


def _robustness_report(raw: Any) -> Optional[dict[str, Any]]:
    """验证节点 ``robustness`` 输出 → 契约 ``robustness_report``（experiment-summary.v1）。

    节点产出两种形状（ValidationNode._execute_checks）：未执行 ``{executed: false,
    reason}``（工具 / 监督者 / 会话出口 / 实验脚本 / 预算任一缺席）；已执行时还带
    过程字段（attempts / llm_calls / summary / failed_checks / final_code_artifact /
    produced_artifacts）。契约七键全必填且 additionalProperties=false：未执行形状
    补齐空值，过程字段剔除（活动流另有展示）。沙盒化之前的运行与模拟节点没有该键
    → null（契约允许）。计数按投影后的 checks 重算，保证 ``checks_total ==
    len(checks)`` 的契约不变量不受个别畸形项被剔除的影响。检验脚本的独立审稿
    （``review``，§8.4）随报告一起投影为可选键。
    """
    if not isinstance(raw, dict):
        return None
    executed = raw.get("executed") is True
    checks: list[dict[str, Any]] = []
    if executed:
        for entry in raw.get("checks") or []:
            if not isinstance(entry, dict):
                continue
            check_id = str(entry.get("id") or "").strip()
            if not check_id or not isinstance(entry.get("passed"), bool):
                continue
            checks.append(
                {
                    "id": check_id,
                    "name": str(entry.get("name") or check_id),
                    "passed": entry["passed"],
                    "value": _number_or_none(entry.get("value")),
                    "threshold": _threshold(entry.get("threshold")),
                    "detail": str(entry.get("detail") or ""),
                    # 该检查针对的模型假设（验证节点已按已知假设 id 归一化，
                    # 这里只挡住非字符串）；通用检查 / 旧运行 → null
                    "assumption_id": _text_or_none(entry.get("assumption_id")),
                }
            )
    status = raw.get("status") if executed else None
    return {
        "executed": executed,
        "status": str(status) if status is not None else None,
        "summary_text": str(raw.get("summary_text") or "") if executed else "",
        "checks": checks,
        "checks_total": len(checks),
        "checks_failed": sum(1 for check in checks if not check["passed"]),
        "reason": str(raw.get("reason") or ""),
        "review": _review_report(raw.get("review")),
    }


def _validation_report(state: Optional[StageState]) -> Optional[dict[str, Any]]:
    if state is None:
        return None
    outputs = state.outputs
    if outputs.get("verdict") not in _VERDICTS:
        # sim 节点没有 verdict；契约 enum 不接受空串兜底，报告整体视为未产出
        return None
    checks = []
    for entry in outputs.get("checks") or []:
        if not isinstance(entry, dict):
            continue
        checks.append(
            {
                "name": str(entry.get("name") or ""),
                "result": str(entry.get("result") or ""),
                "note": str(entry.get("note") or ""),
            }
        )
    return {
        "verdict": str(outputs.get("verdict") or ""),
        "checks": checks,
        "risks": _strs(outputs.get("risks")),
        "validation_summary": str(outputs.get("validation_summary") or ""),
        "robustness": _robustness_report(outputs.get("robustness")),
    }


def _experiment_summary(
    run_id: str,
    experimenting: Optional[StageState],
    validating: Optional[StageState],
) -> Optional[ExperimentSummary]:
    if experimenting is None:
        return None
    outputs = experimenting.outputs
    if "approach_summary" not in outputs:
        # sim 节点/旧运行：不是真实实验节点的产出，正文投影为 null
        return None
    validation = _validation_report(validating)
    updated_at = validating.at if (validating is not None and validating.at is not None) else experimenting.at
    return ExperimentSummary(
        run_id=run_id,
        approach_summary=str(outputs.get("approach_summary") or ""),
        metrics=dict(outputs.get("metrics") or {}),
        stdout_tail=str(outputs.get("stdout_tail") or ""),
        experiment_summary=str(outputs.get("experiment_summary") or ""),
        validation=validation,
        # 实验代码的独立审稿结论（§8.4 生成者-评审者环）；审稿环之前的运行 → null
        review=_review_report(outputs.get("review")),
        updated_at=iso_z(updated_at),
    )


#: 冻结清单条目允许的出处阶段（契约 enum；论文阶段自己不产数字）。
_FROZEN_SOURCE_STAGES = frozenset(
    {"DATA_PREPARATION", "MODEL_PLANNING", "EXPERIMENTING", "VALIDATING"}
)
_AUDIT_FINDING_KINDS = frozenset({"unsourced_number"})


def _frozen_numbers(raw: Any) -> Optional[list[dict[str, Any]]]:
    """节点 outputs.frozen_numbers → 契约 frozen_number[]。

    键不存在（H5 之前的运行、回退单次生成之前的旧草稿）→ null；畸形条目
    （值不是数、缺 id、出处阶段不在 enum）逐条剔除——契约 additionalProperties=false，
    直接透传等于让一条脏数据把整个 stage-outputs 接口打成 500。
    """
    if not isinstance(raw, list):
        return None
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("id") or "").strip()
        value = _number_or_none(item.get("value"))
        stage = str(item.get("source_stage") or "")
        if not entry_id or value is None or stage not in _FROZEN_SOURCE_STAGES:
            continue
        entries.append(
            {
                "id": entry_id,
                "label": str(item.get("label") or entry_id),
                "value": value,
                "source_stage": stage,
                "source_path": str(item.get("source_path") or ""),
            }
        )
    return entries


def _audit_findings(raw: Any) -> Optional[list[dict[str, Any]]]:
    """节点 outputs.audit_findings → 契约 audit_finding[]（同上：缺省 null、畸形剔除）。"""
    if not isinstance(raw, list):
        return None
    findings: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in _AUDIT_FINDING_KINDS:
            continue
        findings.append(
            {
                "scope": str(item.get("scope") or ""),
                "kind": kind,
                "numbers": _strs(item.get("numbers")),
                "detail": str(item.get("detail") or ""),
            }
        )
    return findings


def _document_draft(run_id: str, state: Optional[StageState]) -> Optional[DocumentDraft]:
    if state is None:
        return None
    outputs = state.outputs
    if "title" not in outputs:
        # sim 节点/旧运行：没有论文契约字段，草稿投影为 null
        return None
    sections = []
    for entry in outputs.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        sections.append(
            {
                "heading": str(entry.get("heading") or ""),
                "content": str(entry.get("content") or ""),
            }
        )
    return DocumentDraft(
        run_id=run_id,
        title=str(outputs.get("title") or ""),
        abstract=str(outputs.get("abstract") or ""),
        keywords=_strs(outputs.get("keywords")),
        sections=sections,
        version=max(state.count, 1),
        updated_at=iso_z(state.at),
        frozen_numbers=_frozen_numbers(outputs.get("frozen_numbers")),
        audit_findings=_audit_findings(outputs.get("audit_findings")),
    )


def _artifact_projection(
    row: ArtifactRow, producer_node: Optional[str], blobs: ArtifactBlobStore
) -> dict[str, Any]:
    downloadable = row.status == "READY" and has_readable_local_content(
        blobs, row.uri, row.sha256
    )
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "media_type": row.media_type or "application/octet-stream",
        "size_bytes": row.size_bytes,
        "status": row.status,
        "producer_node": producer_node,
        "download_url": f"/api/v1/artifacts/{row.id}/download" if downloadable else None,
    }


def _delivery_manifest(
    run: TaskRunRow,
    stages: dict[str, StageState],
    step_nodes: dict[str, str],
    artifact_rows: list[ArtifactRow],
    blobs: ArtifactBlobStore,
) -> Optional[DeliveryManifest]:
    analysis = stages.get(_PROBLEM_ANALYSIS)
    experimenting = stages.get(_EXPERIMENTING)
    validating = stages.get(_VALIDATING)
    paper = stages.get(_PAPER_WRITING)

    problem_title = None
    if analysis is not None:
        title = str(analysis.outputs.get("title") or "").strip()
        problem_title = title or None

    # 只有真实实验节点的产出才算「实验已完成」；sim 实验不是真实实验，指标保持 null
    key_metrics = None
    if experimenting is not None and "approach_summary" in experimenting.outputs:
        key_metrics = dict(experimenting.outputs.get("metrics") or {})
    validation_verdict = validating.outputs.get("verdict") if validating is not None else None
    if validation_verdict not in _VERDICTS:
        validation_verdict = None

    artifacts: list[dict[str, Any]] = []
    paper_artifact_id: Optional[str] = None
    paper_artifact_at: Optional[datetime] = None
    for row in artifact_rows:
        producer_node = step_nodes.get(row.producer_step or "")
        artifacts.append(_artifact_projection(row, producer_node, blobs))
        if row.kind == "paper":
            created_at = as_utc(row.created_at)
            if paper_artifact_at is None or (created_at is not None and created_at > paper_artifact_at):
                paper_artifact_id = row.id
                paper_artifact_at = created_at

    paper_citation = None
    if paper is not None and "title" in paper.outputs:
        outputs = paper.outputs
        paper_citation = {
            "title": str(outputs.get("title") or ""),
            "abstract": outputs.get("abstract"),
            "keywords": _strs(outputs.get("keywords")),
            "artifact_id": paper_artifact_id,
        }

    has_content = (
        bool(artifacts)
        or problem_title is not None
        or key_metrics is not None
        or validation_verdict is not None
        or paper_citation is not None
    )
    if not has_content:
        return None

    times = [t for t in (as_utc(run.updated_at),) if t is not None]
    times.extend(t for t in (as_utc(state.at) for state in stages.values()) if t is not None)
    times.extend(t for t in (as_utc(row.created_at) for row in artifact_rows) if t is not None)

    return DeliveryManifest(
        run_id=run.id,
        problem_title=problem_title,
        artifacts=artifacts,
        key_metrics=key_metrics,
        validation_verdict=validation_verdict,
        paper_citation=paper_citation,
        updated_at=iso_z(max(times)) if times else iso_z(run.updated_at),
    )


def build_stage_outputs(
    session: Session, run: TaskRunRow, blobs: ArtifactBlobStore
) -> StageOutputs:
    stages, step_nodes = replay_stage_outputs(session, run.id)
    overlay_stage_output_rows(session, run.id, stages)
    steps = list(
        session.execute(select(StepRunRow).where(StepRunRow.run_id == run.id)).scalars()
    )
    # 交付清单只列每个节点最近一趟的产物：修订重做后旧趟成果不再并列
    artifact_rows = current_pass_artifacts(
        session.execute(
            select(ArtifactRow)
            .where(ArtifactRow.run_id == run.id)
            .order_by(ArtifactRow.created_at.asc(), ArtifactRow.id.asc())
        ).scalars(),
        steps,
    )

    # G1 决策台账进方案投影：审批行是决策的持久事实（resolution 由动作层落库）
    approvals = list(
        session.execute(
            select(ApprovalRequestRow).where(ApprovalRequestRow.run_id == run.id)
        ).scalars()
    )

    return StageOutputs(
        run_id=run.id,
        problem_frame=_problem_frame(run.id, stages.get(_PROBLEM_ANALYSIS)),
        dataset_profile=_dataset_profile(run.id, stages.get(_DATA_PREPARATION)),
        plan_proposal=_plan_proposal(run.id, stages.get(_MODEL_PLANNING), approvals),
        experiment_summary=_experiment_summary(
            run.id, stages.get(_EXPERIMENTING), stages.get(_VALIDATING)
        ),
        document_draft=_document_draft(run.id, stages.get(_PAPER_WRITING)),
        delivery_manifest=_delivery_manifest(run, stages, step_nodes, artifact_rows, blobs),
    )
