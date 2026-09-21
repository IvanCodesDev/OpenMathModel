"""E6 看板：从事件日志聚合运行级 / 批次级的质量、失败与成本指标（设计 §14.1 E6、§14.2、§3.3 F5）。

立场与边界：

- **数字只来自事件**。输入是一条运行的领域事件（``AgentEvent`` 或其 ``to_dict()`` 字典，
  也接受控制面 run_domain_events 行的 ``{event_type, payload, seq, created_at}`` 形状）
  加可选的**过程事件**——生产 run.log 里 ``kind = llm_call / llm_call_started /
  llm_call_failed / budget_limit / …`` 的 payload，以及子代理审计 ``tool = "subagent:<kind>"``
  ——评测会话没有过程事件时，模型调用面如实标 ``available: False``，不用别处的数字补。
- **纯函数、零接线**：不读库、不碰装配点；TraceHub 生产接线仍随 E6 批次另议，本模块只把
  「事件 → 指标」这一步做成可测的代码，evals 与控制面都能复用。
- **错误码按目录归类**：``STEP_FAILED.error`` 文首的 ``[Exxx]``（``AgentError`` 的固定格式）
  或 payload 的 ``error_code`` → ``omm_agent_core.errors.CATALOG`` 的 owner / disposition，
  没有码的失败如实计 ``uncoded``；目录外的码保留原样、归属记 None，不猜。
- 需要人工标注或多趟对比的指标（读题一致率、方案认可率、闸门精准率）**不在这里编造**——
  能从事件推出的代理量（如「人是否采纳了系统推荐」）单列并写明推导。

每个指标的推导写在对应 ``_*_section`` 的 docstring；报告结构见 ``aggregate_run``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from omm_agent_core import INTERRUPTED_STEP_ERROR, WORK_SEQUENCE, AgentEvent, EventType
from omm_agent_core.errors import CATALOG, ErrorCode

__all__ = [
    "REPORT_VERSION",
    "SANDBOX_TOOLS",
    "aggregate_batch",
    "aggregate_run",
    "compare_reports",
    "render_batch_markdown",
    "render_markdown",
]

#: 报告形状版本：加键不升版，删键 / 改语义才升。
REPORT_VERSION = 1

#: 计作「沙盒运行」的工具名：python_run 是 Tier0 底座，code_run 是多语言统一入口（H7）。
SANDBOX_TOOLS: tuple[str, ...] = ("python_run", "code_run")

#: 知识库两只读工具（薄版 knowledge_search，§10.3）。
KNOWLEDGE_TOOLS: tuple[str, ...] = ("knowledge_search", "knowledge_read")

_ERROR_CODE = re.compile(r"\[(E\d{3})\]")
_LAST_ERROR_LIMIT = 300
#: 能携带迭代许可字段（via_edge / iteration / max_iters / graph）的事件（D2.2）。
_LICENSED_EVENT_TYPES = (
    EventType.RUN_REDO.value,
    EventType.REVIEW_RESOLVED.value,
    EventType.REVISION_REQUESTED.value,
)
_STATE_ORDER = [state.value for state in WORK_SEQUENCE]
_EVENT_TYPES = {member.value for member in EventType}
_ENVELOPE_STATUSES = ("done", "failed", "exhausted", "timeout")


# -- 输入规整 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ev:
    seq: int
    type: str
    payload: Mapping[str, Any]
    created_at: str | None
    run_id: str | None = None


def _normalize(events: Iterable[Any]) -> list[_Ev]:
    """AgentEvent / 字典混合输入 → 按 seq 稳定排序的统一形状；认不出的项跳过。"""
    items: list[_Ev] = []
    for index, raw in enumerate(events):
        if isinstance(raw, AgentEvent):
            items.append(
                _Ev(int(raw.seq), raw.event_type.value, raw.payload, raw.created_at, raw.run_id)
            )
            continue
        if not isinstance(raw, Mapping):
            continue
        event_type = raw.get("event_type") or raw.get("type")
        if not isinstance(event_type, str) or event_type not in _EVENT_TYPES:
            continue
        payload = raw.get("payload")
        seq = raw.get("seq", raw.get("sequence"))
        created_at = raw.get("created_at")
        run_id = raw.get("run_id")
        items.append(
            _Ev(
                _int(seq, default=index + 1),
                event_type,
                payload if isinstance(payload, Mapping) else {},
                created_at if isinstance(created_at, str) else None,
                str(run_id) if run_id else None,
            )
        )
    items.sort(key=lambda item: item.seq)
    return items


def _int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in _sequence(value) if isinstance(item, Mapping)]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds_between(start: str | None, end: str | None) -> float | None:
    a, b = _parse_time(start), _parse_time(end)
    if a is None or b is None:
        return None
    try:
        return round((b - a).total_seconds(), 3)
    except TypeError:  # naive 与 aware 混用
        return None


def _rate(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _codes_in(payload: Mapping[str, Any]) -> list[str]:
    """失败载荷里的错误码：显式 ``error_code`` 优先，否则取文案里第一处 ``[Exxx]``。"""
    explicit = payload.get("error_code")
    if isinstance(explicit, str) and _ERROR_CODE.fullmatch(f"[{explicit}]"):
        return [explicit]
    error = payload.get("error")
    if isinstance(error, str):
        found = _ERROR_CODE.findall(error)
        if found:
            return [found[0]]
    return []


def _catalog_entry(code: str) -> dict[str, Any]:
    try:
        info = CATALOG[ErrorCode(code)]
    except ValueError:
        return {"owner": None, "disposition": None, "summary": None}
    return {"owner": info.owner, "disposition": info.disposition.value, "summary": info.summary}


def _sorted_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    return {key: data[key] for key in sorted(data)}


def _ordered_by_state(data: Mapping[str, Any]) -> dict[str, Any]:
    known = [state for state in _STATE_ORDER if state in data]
    extra = sorted(key for key in data if key not in _STATE_ORDER)
    return {key: data[key] for key in known + extra}


# -- 步骤索引（其余各节共用） ----------------------------------------------------------------


@dataclass
class _Step:
    step_id: str
    state: str
    attempt: int
    started_at: str | None
    ended_at: str | None = None
    status: str = "RUNNING"  # SUCCEEDED | FAILED | RUNNING
    outputs: Mapping[str, Any] | None = None
    metrics: Mapping[str, Any] | None = None


def _index_steps(events: Sequence[_Ev]) -> dict[str, _Step]:
    steps: dict[str, _Step] = {}
    for event in events:
        payload = event.payload
        step_id = str(payload.get("step_id") or "")
        if event.type == EventType.STEP_STARTED.value:
            state = str(payload.get("state") or "UNKNOWN")
            steps[step_id or f"step@{event.seq}"] = _Step(
                step_id=step_id, state=state, attempt=_int(payload.get("attempt"), 1),
                started_at=event.created_at,
            )
        elif event.type in (EventType.STEP_SUCCEEDED.value, EventType.STEP_FAILED.value):
            step = steps.get(step_id)
            if step is None:
                # 只见到收尾没见到开步（截断的日志）：仍然登记，状态未知
                step = _Step(step_id=step_id, state="UNKNOWN", attempt=1, started_at=None)
                steps[step_id or f"step@{event.seq}"] = step
            step.ended_at = event.created_at
            if event.type == EventType.STEP_SUCCEEDED.value:
                step.status = "SUCCEEDED"
                step.outputs = _mapping(payload.get("outputs"))
                step.metrics = _mapping(payload.get("metrics"))
            else:
                step.status = "FAILED"
    return steps


def _state_of(steps: Mapping[str, _Step], step_id: Any) -> str:
    step = steps.get(str(step_id or ""))
    return step.state if step is not None else "UNKNOWN"


# -- 各节 ------------------------------------------------------------------------------------


def _run_section(events: Sequence[_Ev]) -> dict[str, Any]:
    """运行面：结果 = 最后一个终态事件（RUN_COMPLETED → completed；RUN_FAILED → failed，
    紧跟 RUN_CANCELLED 之后的 RUN_FAILED → cancelled）；终态之后又有推进（修订轮开步）→
    in_progress。``graph`` 取任一许可字段里的图 id（v1 图不带 → None）。时长 = 首尾事件时间差。"""
    project_id = None
    outcome = "in_progress"
    final_state: str | None = None
    failed_state: str | None = None
    graph: str | None = None
    previous_type: str | None = None
    for event in events:
        payload = event.payload
        if event.type == EventType.RUN_CREATED.value:
            project_id = payload.get("project_id")
            final_state = "CREATED"
        elif event.type == EventType.STATE_CHANGED.value:
            final_state = str(payload.get("to") or final_state)
            outcome = "in_progress"
        elif event.type == EventType.REVIEW_REQUESTED.value:
            final_state = "NEEDS_REVIEW"
            outcome = "in_progress"
        elif event.type in (EventType.REVISION_REQUESTED.value, EventType.RUN_RETRIED.value,
                            EventType.RUN_REDO.value, EventType.STEP_STARTED.value):
            outcome = "in_progress"
            failed_state = None  # 重试 / 回退后运行继续，之前的失败不再是「当前失败」
            if event.type == EventType.STEP_STARTED.value:
                final_state = str(payload.get("state") or final_state)
        elif event.type == EventType.REVIEW_RESOLVED.value:
            if payload.get("approved"):
                final_state = str(payload.get("resume_state") or final_state)
            outcome = "in_progress"
        elif event.type == EventType.RUN_COMPLETED.value:
            outcome, final_state = "completed", "COMPLETED"
        elif event.type == EventType.RUN_FAILED.value:
            outcome = "cancelled" if previous_type == EventType.RUN_CANCELLED.value else "failed"
            final_state = "FAILED"
            failed_state = str(payload.get("failed_state") or "") or None
        if graph is None and isinstance(payload.get("graph"), str) and payload.get("via_edge"):
            graph = str(payload["graph"])
        previous_type = event.type
    started_at = events[0].created_at if events else None
    ended_at = events[-1].created_at if events else None
    return {
        "run_id": next((event.run_id for event in events if event.run_id), None),
        "project_id": project_id,
        "outcome": outcome,
        "final_state": final_state,
        "failed_state": failed_state,
        "graph": graph,
        "events": len(events),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": _seconds_between(started_at, ended_at),
    }


def _stage_section(steps: Mapping[str, _Step]) -> dict[str, Any]:
    """阶段面：每个工作态的尝试数（STEP_STARTED）、成败（STEP_SUCCEEDED / STEP_FAILED）与节点自报
    用量——``metrics.llm_attempts``（方案节点写在 ``outputs.llm_attempts``，两处都认）、
    ``code_rounds`` / ``waves``（沙盒 R2 波次）、``review_rounds``（审稿轮）、``quality_warnings``
    条数；``duration_ms`` = 各步骤开步到收尾的时间差之和（缺时间戳的步骤不计）。"""
    stages: dict[str, dict[str, Any]] = {}
    for step in steps.values():
        row = stages.setdefault(step.state, {
            "attempts": 0, "succeeded": 0, "failed": 0, "llm_attempts": 0, "code_rounds": 0,
            "waves": 0, "review_rounds": 0, "quality_warnings": 0, "duration_ms": None,
        })
        row["attempts"] += 1
        if step.status == "SUCCEEDED":
            row["succeeded"] += 1
        elif step.status == "FAILED":
            row["failed"] += 1
        metrics = step.metrics or {}
        outputs = step.outputs or {}
        row["llm_attempts"] += _int(metrics.get("llm_attempts", outputs.get("llm_attempts")))
        row["code_rounds"] += _int(metrics.get("code_rounds"))
        row["waves"] += _int(metrics.get("waves"))
        row["review_rounds"] += _int(metrics.get("review_rounds"))
        row["quality_warnings"] += len(_sequence(metrics.get("quality_warnings")))
        elapsed = _seconds_between(step.started_at, step.ended_at)
        if elapsed is not None:
            row["duration_ms"] = int((row["duration_ms"] or 0) + elapsed * 1000)
    return _ordered_by_state(stages)


def _gate_row() -> dict[str, int]:
    return {"requested": 0, "approved": 0, "rejected": 0, "rollbacks": 0,
            "recommended_available": 0, "recommended_followed": 0}


def _recommended_option(gate: Mapping[str, Any]) -> str | None:
    for option in _sequence(gate.get("options")):
        option = _mapping(option)
        if option.get("recommended") and option.get("id"):
            return str(option["id"])
    recommended = _mapping(gate.get("impact")).get("recommended")
    return str(recommended) if recommended else None


def _gate_section(events: Sequence[_Ev]) -> dict[str, Any]:
    """闸门面：REVIEW_REQUESTED 按 ``gate.gate``（G1–G4）归类，节点没声明闸门元数据的记
    ``undeclared``；REVISION_REQUESTED 是修订门（``revision``）。与下一条 REVIEW_RESOLVED 配对：
    ``approved`` 分批准 / 拒绝；批准且 ``rerun`` 或落点不等于提门阶段 → ``rollbacks``（回退重做）。
    「采纳推荐」= 批准时 ``reason``（所选 option id）等于卡片推荐项（``options[].recommended``
    或 ``impact.recommended``）——闸门精准率需要人工标注，这里只给这个可从事件推出的代理量。
    ``wait_s`` = 提门到拍板的时间差之和（未拍板的门不计；没有任何拍板 → None）。"""
    by_gate: dict[str, dict[str, int]] = {}
    pending: dict[str, Any] | None = None
    totals = _gate_row()
    wait_total = 0.0
    waited = False
    for event in events:
        payload = event.payload
        if event.type in (EventType.REVIEW_REQUESTED.value, EventType.REVISION_REQUESTED.value):
            if event.type == EventType.REVISION_REQUESTED.value:
                gate_id = "revision"
                recommended = None
                resume_state = str(payload.get("target_state") or "")
            else:
                gate = _mapping(payload.get("gate"))
                gate_id = str(gate.get("gate") or "") or "undeclared"
                recommended = _recommended_option(gate)
                resume_state = str(payload.get("resume_state") or "")
            row = by_gate.setdefault(gate_id, _gate_row())
            row["requested"] += 1
            totals["requested"] += 1
            if recommended:
                row["recommended_available"] += 1
                totals["recommended_available"] += 1
            pending = {"gate": gate_id, "recommended": recommended, "resume_state": resume_state,
                       "at": event.created_at}
        elif event.type == EventType.REVIEW_RESOLVED.value:
            gate_id = pending["gate"] if pending else "undeclared"
            row = by_gate.setdefault(gate_id, _gate_row())
            approved = bool(payload.get("approved"))
            key = "approved" if approved else "rejected"
            row[key] += 1
            totals[key] += 1
            if approved and pending is not None:
                resume_state = str(payload.get("resume_state") or "")
                # 修订门批准 = 已完成的运行回到某阶段重做，天然是回退
                rollback = (
                    gate_id == "revision"
                    or bool(payload.get("rerun"))
                    or (bool(resume_state) and resume_state != pending["resume_state"])
                )
                if rollback:
                    row["rollbacks"] += 1
                    totals["rollbacks"] += 1
                reason = str(payload.get("reason") or "")
                if pending["recommended"] and reason == pending["recommended"]:
                    row["recommended_followed"] += 1
                    totals["recommended_followed"] += 1
            if pending is not None:
                elapsed = _seconds_between(pending["at"], event.created_at)
                if elapsed is not None:
                    wait_total += elapsed
                    waited = True
            pending = None
    return {
        **totals,
        "resolved": totals["approved"] + totals["rejected"],
        "wait_s": round(wait_total, 3) if waited else None,
        "by_gate": _sorted_dict(by_gate),
    }


def _iteration_section(events: Sequence[_Ev], gates: Mapping[str, Any]) -> dict[str, Any]:
    """回退与迭代面：RUN_REDO 计回退（``auto`` 区分图的条件边自动回退与人工 redo）；许可字段
    ``via_edge``（RUN_REDO / 回退的 REVIEW_RESOLVED / REVISION_REQUESTED 都带）按边计数；
    ``exhausted`` = REVIEW_REQUESTED 带 ``iteration.code == E430``（自动回退轮次用尽才开的门）；
    ``rollbacks`` 与闸门面同源；``revisions`` = REVISION_REQUESTED 条数、``revision_round_max`` 取其
    ``round`` 最大值；``retries`` = RUN_RETRIED 条数。"""
    redo_total = auto = exhausted = revisions = retries = 0
    revision_round_max = 0
    by_edge: dict[str, int] = {}
    for event in events:
        payload = event.payload
        if event.type == EventType.RUN_REDO.value:
            redo_total += 1
            if payload.get("auto"):
                auto += 1
        elif event.type == EventType.REVIEW_REQUESTED.value:
            refused_code = str(_mapping(payload.get("iteration")).get("code") or "")
            if refused_code == ErrorCode.GRAPH_ITERATION_LIMIT.value:
                exhausted += 1
        elif event.type == EventType.REVISION_REQUESTED.value:
            revisions += 1
            revision_round_max = max(revision_round_max, _int(payload.get("round")))
        elif event.type == EventType.RUN_RETRIED.value:
            retries += 1
        edge = payload.get("via_edge")
        if isinstance(edge, str) and edge and event.type in _LICENSED_EVENT_TYPES:
            by_edge[edge] = by_edge.get(edge, 0) + 1
    return {
        "redo_total": redo_total,
        "auto": auto,
        "manual": redo_total - auto,
        "by_edge": _sorted_dict(by_edge),
        "exhausted": exhausted,
        "rollbacks": int(gates.get("rollbacks") or 0),
        "revisions": revisions,
        "revision_round_max": revision_round_max,
        "retries": retries,
    }


def _failure_section(events: Sequence[_Ev], steps: Mapping[str, _Step]) -> dict[str, Any]:
    """失败面：STEP_FAILED 逐条计数并按错误码 / 阶段归类；RUN_FAILED 只在**不是**紧随
    STEP_FAILED（fail_run / 取消）时另计一条码，避免同一失败重复归码。码来自 ``error_code``
    或文案 ``[Exxx]``，经 CATALOG 映射 owner / disposition；``uncoded`` = 没有码的失败；
    ``interrupted`` = 进程重启留下的悬挂步骤（``INTERRUPTED_STEP_ERROR``）；``by_class``
    只在载荷自带 ``failure_class`` 时出现。"""
    steps_failed = run_failed = cancelled = interrupted = uncoded = 0
    by_code: dict[str, dict[str, Any]] = {}
    by_state: dict[str, int] = {}
    by_class: dict[str, int] = {}
    by_disposition: dict[str, int] = {}
    last_error: str | None = None
    previous_type: str | None = None

    def classify(payload: Mapping[str, Any], state: str) -> None:
        nonlocal uncoded, last_error
        codes = _codes_in(payload)
        if not codes:
            uncoded += 1
        for code in codes:
            row = by_code.setdefault(code, {"count": 0, **_catalog_entry(code)})
            row["count"] += 1
            disposition = row["disposition"] or "unknown"
            by_disposition[disposition] = by_disposition.get(disposition, 0) + 1
        by_state[state] = by_state.get(state, 0) + 1
        failure_class = payload.get("failure_class")
        if isinstance(failure_class, str) and failure_class:
            by_class[failure_class] = by_class.get(failure_class, 0) + 1
        error = payload.get("error")
        if isinstance(error, str) and error:
            last_error = error[:_LAST_ERROR_LIMIT]

    for event in events:
        payload = event.payload
        if event.type == EventType.STEP_FAILED.value:
            steps_failed += 1
            if payload.get("error") == INTERRUPTED_STEP_ERROR:
                interrupted += 1
            classify(payload, _state_of(steps, payload.get("step_id")))
        elif event.type == EventType.RUN_FAILED.value:
            run_failed += 1
            if previous_type != EventType.STEP_FAILED.value:
                classify(payload, str(payload.get("failed_state") or "UNKNOWN"))
        elif event.type == EventType.RUN_CANCELLED.value:
            cancelled += 1
        previous_type = event.type
    return {
        "steps_failed": steps_failed,
        "run_failed": run_failed,
        "cancelled": cancelled,
        "interrupted": interrupted,
        "by_code": _sorted_dict(by_code),
        "by_disposition": _sorted_dict(by_disposition),
        "uncoded": uncoded,
        "by_state": _ordered_by_state(by_state),
        "by_class": _sorted_dict(by_class),
        "last_error": last_error,
    }


def _subagent_row() -> dict[str, int]:
    return {"spawned": 0, "done": 0, "failed": 0, "exhausted": 0, "timeout": 0}


def _count_subagent(rows: dict[str, dict[str, int]], payload: Mapping[str, Any]) -> bool:
    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool.startswith("subagent:"):
        return False
    kind = tool[len("subagent:"):] or "unknown"
    row = rows.setdefault(kind, _subagent_row())
    phase = payload.get("phase")
    if phase == "spawn":
        row["spawned"] += 1
    elif phase == "result":
        status = str(payload.get("envelope_status") or "")
        if status in _ENVELOPE_STATUSES:
            row[status] += 1
    return True


def _tool_section(
    events: Sequence[_Ev], process_events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """工具面：TOOL_CALLED 按工具名计调用 / 失败（``status != succeeded``）/ 累计时长；
    ``sandbox`` 只看 ``SANDBOX_TOOLS``（python_run / code_run）——即「沙盒运行次数」；
    ``knowledge`` 只看知识库两工具（检索**命中率**要读结果正文，事件里没有，不编）；
    ``subagents`` 来自子代理审计（``tool = subagent:<kind>``，spawn / result 两相），
    生产在 run.log 过程事件里、worker 装配可能落在 TOOL_CALLED——两路都认，调用方传其一。"""
    by_tool: dict[str, dict[str, int]] = {}
    subagents: dict[str, dict[str, int]] = {}
    for event in events:
        if event.type != EventType.TOOL_CALLED.value:
            continue
        payload = event.payload
        if _count_subagent(subagents, payload):
            continue
        tool = str(payload.get("tool") or "unknown")
        row = by_tool.setdefault(tool, {"calls": 0, "failed": 0, "duration_ms": 0})
        row["calls"] += 1
        if str(payload.get("status") or "succeeded") != "succeeded":
            row["failed"] += 1
        row["duration_ms"] += _int(payload.get("duration_ms"))
    for payload in process_events:
        _count_subagent(subagents, payload)
    sandbox = {
        tool: {"calls": row["calls"], "failed": row["failed"]}
        for tool, row in by_tool.items()
        if tool in SANDBOX_TOOLS
    }
    knowledge = [row for tool, row in by_tool.items() if tool in KNOWLEDGE_TOOLS]
    return {
        "calls": sum(row["calls"] for row in by_tool.values()),
        "failed": sum(row["failed"] for row in by_tool.values()),
        "by_tool": _sorted_dict(by_tool),
        "sandbox": {
            "runs": sum(row["calls"] for row in sandbox.values()),
            "failed": sum(row["failed"] for row in sandbox.values()),
            "by_tool": _sorted_dict(sandbox),
        },
        "knowledge": {
            "calls": sum(row["calls"] for row in knowledge),
            "failed": sum(row["failed"] for row in knowledge),
        },
        "subagents": _sorted_dict(subagents),
    }


#: 生成者-评审者环的结论在各阶段产出里的位置（§8.4 四个沙盒消费方）。
_REVIEW_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DATA_PREPARATION", ("outputs", "cleaning", "review")),
    ("EXPERIMENTING", ("outputs", "review")),
    ("VALIDATING", ("outputs", "robustness", "review")),
    ("PAPER_WRITING", ("metrics", "figure_review")),
)


def _dig(root: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = root
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _review_row() -> dict[str, int]:
    return {"loops": 0, "executed": 0, "first_round_accept": 0, "reject_rounds": 0,
            "static_reject_rounds": 0, "stalemates": 0, "blockers": 0, "findings": 0,
            "rerun_inconsistent": 0, "not_executed": 0}


def _review_section(steps: Mapping[str, _Step]) -> dict[str, Any]:
    """生成者-评审者面（§8.4）：每个成功步骤产出里的审稿结论算一环——实验 ``outputs.review``、
    检验 ``outputs.robustness.review``、清洗 ``outputs.cleaning.review``、
    补图 ``metrics.figure_review``。``executed`` 为真才算真审过；
    ``first_round_accept`` = 一轮即 accept 且未僵持；``reject_rounds`` = ``rounds − 1``
    （每多一轮就是一次驳回 → 修复波），静态检查驳回另计 ``static_reject_rounds``；
    ``stalemates`` = 到预算 / 轮次用尽仍有阻断意见（进闸门）；``blockers`` / ``findings``
    取最终裁决；``rerun_inconsistent`` = 节点确定性复跑与首跑指标不一致。
    「Reviewer 捕获率」需要注入 bug 的标注集，这里只给驳回与僵持的发生数。"""
    totals = _review_row()
    by_stage: dict[str, dict[str, int]] = {}
    for step in steps.values():
        if step.status != "SUCCEEDED":
            continue
        root = {"outputs": step.outputs or {}, "metrics": step.metrics or {}}
        for state, path in _REVIEW_PATHS:
            if step.state != state:
                continue
            review = _dig(root, path)
            if not isinstance(review, Mapping):
                continue
            if "executed" not in review and "verdict" not in review:
                continue
            row = by_stage.setdefault(state, _review_row())
            rounds = _int(review.get("rounds"))
            stalemate = bool(review.get("stalemate"))
            accepted_first = (
                rounds <= 1 and str(review.get("verdict") or "") == "accept" and not stalemate
            )
            for target in (row, totals):
                target["loops"] += 1
                if review.get("executed"):
                    target["executed"] += 1
                    if accepted_first:
                        target["first_round_accept"] += 1
                else:
                    target["not_executed"] += 1
                target["reject_rounds"] += max(rounds - 1, 0)
                target["static_reject_rounds"] += _int(review.get("static_rounds"))
                target["stalemates"] += int(stalemate)
                target["blockers"] += _int(review.get("blockers"))
                target["findings"] += len(_sequence(review.get("findings")))
                rerun = _mapping(review.get("rerun"))
                if rerun.get("executed") and not rerun.get("consistent"):
                    target["rerun_inconsistent"] += 1
    return {**totals, "by_stage": _ordered_by_state(by_stage)}


def _succeeded_steps(steps: Mapping[str, _Step], state: str) -> list[_Step]:
    return [step for step in steps.values() if step.state == state and step.status == "SUCCEEDED"]


def _robustness_section(steps: Mapping[str, _Step]) -> dict[str, Any]:
    """稳健性面：取最后一个成功的 VALIDATING 步骤的 ``outputs.robustness``——复跑是否执行、
    检查通过率、``round_comparison`` 三桶计数（回退重做后的复检才有，否则 None）；``rounds`` =
    成功的检验步骤数（每次回退重做一次）。"""
    validating = _succeeded_steps(steps, "VALIDATING")
    empty = {
        "executed": False, "status": None, "checks_total": 0, "checks_failed": 0,
        "pass_rate": None, "uncovered_focus": 0, "rounds": len(validating),
        "round_comparison": None,
    }
    if not validating:
        return empty
    robustness = _mapping((validating[-1].outputs or {}).get("robustness"))
    if not robustness:
        return empty
    total = _int(robustness.get("checks_total"))
    failed = _int(robustness.get("checks_failed"))
    comparison = _mapping(robustness.get("round_comparison"))
    return {
        "executed": bool(robustness.get("executed")),
        "status": str(robustness.get("status")) if robustness.get("status") is not None else None,
        "checks_total": total,
        "checks_failed": failed,
        "pass_rate": _rate(total - failed, total),
        "uncovered_focus": len(_sequence(robustness.get("uncovered_focus"))),
        "rounds": len(validating),
        "round_comparison": (
            {
                "resolved": len(_sequence(comparison.get("resolved"))),
                "still_failing": len(_sequence(comparison.get("still_failing"))),
                "not_rechecked": len(_sequence(comparison.get("not_rechecked"))),
            }
            if comparison
            else None
        ),
    }


def _audit_section(steps: Mapping[str, _Step]) -> dict[str, Any]:
    """终稿审计面（§8.4 审计者链、F9）：取最后一个成功的 PAPER_WRITING 步骤——
    ``audit_findings`` 总数与按 ``kind`` 分布（目标 0）、冻结清单条数、图件清单 / 已插入、
    引用库 / 已引用 / 已验证（``verification ∈ {source_verified, title_matched}``）。
    没有论文步骤 → ``available: False``。"""
    papers = _succeeded_steps(steps, "PAPER_WRITING")
    if not papers:
        return {
            "available": False, "findings_total": 0, "by_kind": {}, "frozen_numbers": 0,
            "figures_total": 0, "figures_inserted": 0, "references_total": 0,
            "references_cited": 0, "references_verified": 0,
        }
    outputs = papers[-1].outputs or {}
    findings = _mappings(outputs.get("audit_findings"))
    by_kind: dict[str, int] = {}
    for finding in findings:
        kind = str(finding.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    figures = _mappings(outputs.get("figures"))
    references = _mappings(outputs.get("references"))
    verified = ("source_verified", "title_matched")
    return {
        "available": True,
        "findings_total": len(findings),
        "by_kind": _sorted_dict(by_kind),
        "frozen_numbers": len(_sequence(outputs.get("frozen_numbers"))),
        "figures_total": len(figures),
        "figures_inserted": sum(1 for item in figures if item.get("inserted")),
        "references_total": len(references),
        "references_cited": sum(1 for item in references if item.get("cited")),
        "references_verified": sum(
            1 for item in references if item.get("verification") in verified
        ),
    }


def _prompt_row() -> dict[str, int]:
    return {
        "calls": 0, "repair_calls": 0, "failed": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "elapsed_ms": 0,
    }


def _llm_section(
    process_events: Sequence[Mapping[str, Any]],
    schema_failures: int,
    prices: Mapping[str, Mapping[str, float]] | None,
) -> dict[str, Any]:
    """模型调用面（只来自生产过程事件）：``llm_call_started`` 计发起（``repair`` 为真即一次结构修复
    → 上一次输出违约），``llm_call`` 计成功返回并累计 tokens / 时长（``tokens_estimated`` 与
    ``answer_from_reasoning`` 单独计数），``llm_call_failed`` 计传输层失败。
    **结构违约率** = repair_calls / started（没有 started 事件的旧日志按 llm_call 计）；
    **修复成功率** = (repair_calls − E120 失败数) / repair_calls——每一次 E120 都是一次没救回的修复
    （错误码目录「修复后仍失败」），没有修复就没有这个率（None）。
    费用只在给了单价（USD / 1M tokens，``{model: {input, output}}``）且型号匹配时计算；没定价的型号
    如实列出，不用别的型号的价格替。"""
    by_prompt: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}
    by_task_kind: dict[str, int] = {}
    started = calls = failed = repair_calls = estimated = from_reasoning = 0
    prompt_tokens = completion_tokens = elapsed_ms = 0
    for payload in process_events:
        kind = payload.get("kind")
        if kind not in ("llm_call_started", "llm_call", "llm_call_failed"):
            continue
        prompt_id = str(payload.get("prompt_id") or "unknown")
        row = by_prompt.setdefault(prompt_id, _prompt_row())
        if kind == "llm_call_started":
            started += 1
            if payload.get("repair"):
                repair_calls += 1
                row["repair_calls"] += 1
        elif kind == "llm_call_failed":
            failed += 1
            row["failed"] += 1
        else:
            calls += 1
            p_tokens = _int(payload.get("prompt_tokens"))
            c_tokens = _int(payload.get("completion_tokens"))
            ms = _int(payload.get("elapsed_ms"))
            row["calls"] += 1
            row["prompt_tokens"] += p_tokens
            row["completion_tokens"] += c_tokens
            row["elapsed_ms"] += ms
            prompt_tokens += p_tokens
            completion_tokens += c_tokens
            elapsed_ms += ms
            estimated += int(bool(payload.get("tokens_estimated")))
            from_reasoning += int(bool(payload.get("answer_from_reasoning")))
            model = str(payload.get("model") or "unknown")
            model_row = by_model.setdefault(
                model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            model_row["calls"] += 1
            model_row["prompt_tokens"] += p_tokens
            model_row["completion_tokens"] += c_tokens
            task_kind = payload.get("task_kind")
            if isinstance(task_kind, str) and task_kind:
                by_task_kind[task_kind] = by_task_kind.get(task_kind, 0) + 1
    if started == 0:
        started = calls
    cost: float | None = None
    unpriced: list[str] = []
    if prices is not None and by_model:
        cost = 0.0
        for model, row in by_model.items():
            price = _mapping(prices.get(model))
            unit_in, unit_out = _float(price.get("input")), _float(price.get("output"))
            if unit_in is None or unit_out is None:
                unpriced.append(model)
                continue
            spent = row["prompt_tokens"] * unit_in + row["completion_tokens"] * unit_out
            cost += spent / 1_000_000
        cost = round(cost, 6)
    return {
        "available": bool(by_prompt),
        "started": started,
        "calls": calls,
        "failed_calls": failed,
        "repair_calls": repair_calls,
        "violation_rate": _rate(repair_calls, started) if by_prompt else None,
        "schema_failures": schema_failures,
        "repair_success_rate": _rate(max(repair_calls - schema_failures, 0), repair_calls),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "elapsed_ms": elapsed_ms,
        "estimated_token_calls": estimated,
        "answers_from_reasoning": from_reasoning,
        "by_prompt": _sorted_dict(by_prompt),
        "by_model": _sorted_dict(by_model),
        "by_task_kind": _sorted_dict(by_task_kind),
        "cost_usd": cost,
        "unpriced_models": sorted(unpriced),
    }


def _process_section(process_events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """过程事件面：按 ``kind`` 计数，另点名三类运维事实——预算硬限触发（``budget_limit``）、
    自动回退轮次用尽（``auto_redo_exhausted``）、执行进程重启（``executor_restarted``）。"""
    by_kind: dict[str, int] = {}
    for payload in process_events:
        kind = payload.get("kind")
        if isinstance(kind, str) and kind:
            by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "events": len(process_events),
        "by_kind": _sorted_dict(by_kind),
        "budget_limit": by_kind.get("budget_limit", 0),
        "auto_redo_exhausted": by_kind.get("auto_redo_exhausted", 0),
        "executor_restarted": by_kind.get("executor_restarted", 0),
    }


def aggregate_run(
    events: Iterable[Any],
    process_events: Iterable[Any] = (),
    *,
    prices: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    """一条运行的 E6 报告（JSON 安全的纯字典）。

    ``events``：领域事件（AgentEvent 或字典）；``process_events``：生产 run.log 的过程事件
    payload（可省）；``prices``：型号单价（USD / 1M tokens），只用于费用。

    顶层键：``version`` / ``run`` / ``stages`` / ``gates`` / ``iterations`` / ``failures`` /
    ``tools`` / ``reviews`` / ``robustness`` / ``audit`` / ``llm`` / ``process``——各节含义见
    对应 ``_*_section``。
    """
    normalized = _normalize(events)
    process = [item for item in process_events if isinstance(item, Mapping)]
    steps = _index_steps(normalized)
    gates = _gate_section(normalized)
    failures = _failure_section(normalized, steps)
    schema_row = _mapping(failures["by_code"].get(ErrorCode.LLM_SCHEMA_VIOLATION.value))
    schema_failures = _int(schema_row.get("count"))
    return {
        "version": REPORT_VERSION,
        "run": _run_section(normalized),
        "stages": _stage_section(steps),
        "gates": gates,
        "iterations": _iteration_section(normalized, gates),
        "failures": failures,
        "tools": _tool_section(normalized, process),
        "reviews": _review_section(steps),
        "robustness": _robustness_section(steps),
        "audit": _audit_section(steps),
        "llm": _llm_section(process, schema_failures, prices),
        "process": _process_section(process),
    }


# -- 批次汇总与漂移对照 ------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def aggregate_batch(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """一批运行报告（``aggregate_run`` 的输出）→ E6 数据集回归的批次视图（§14.1 E6 行）。

    计数直接求和；比率**用求和后的分子分母重算**（不是各运行比率的平均，避免小样本运行放大权重）；
    费用与时长只对有值的运行求和 / 求均值，一趟都没有就是 None。``audit_clean_rate`` = 有终稿审计的
    运行里发现数为 0 的占比（审计违规数目标 0 的批次口径）。"""
    outcomes = {"completed": 0, "failed": 0, "cancelled": 0, "in_progress": 0}
    totals: dict[str, Any] = {
        "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cost_usd": None, "sandbox_runs": 0, "sandbox_failed": 0, "steps_failed": 0,
        "redo_total": 0, "auto_redo": 0, "retries": 0, "revisions": 0, "gates_requested": 0,
        "rollbacks": 0, "review_loops": 0, "review_executed": 0, "review_first_round_accept": 0,
        "review_stalemates": 0, "audit_findings_total": 0, "quality_warnings": 0,
        "duration_s": None,
    }
    recommended_followed = recommended_available = 0
    started = repair_calls = schema_failures = 0
    audits = audits_clean = 0
    failures_by_code: dict[str, int] = {}
    tools_by_tool: dict[str, int] = {}
    durations: list[float] = []
    costs: list[float] = []
    tokens: list[float] = []
    llm_calls: list[float] = []
    for report in reports:
        run = _mapping(report.get("run"))
        outcome = str(run.get("outcome") or "in_progress")
        outcomes[outcome if outcome in outcomes else "in_progress"] += 1
        duration = _float(run.get("duration_s"))
        if duration is not None:
            durations.append(duration)
        llm = _mapping(report.get("llm"))
        totals["llm_calls"] += _int(llm.get("calls"))
        totals["prompt_tokens"] += _int(llm.get("prompt_tokens"))
        totals["completion_tokens"] += _int(llm.get("completion_tokens"))
        totals["total_tokens"] += _int(llm.get("total_tokens"))
        tokens.append(float(_int(llm.get("total_tokens"))))
        llm_calls.append(float(_int(llm.get("calls"))))
        started += _int(llm.get("started"))
        repair_calls += _int(llm.get("repair_calls"))
        schema_failures += _int(llm.get("schema_failures"))
        cost = _float(llm.get("cost_usd"))
        if cost is not None:
            costs.append(cost)
        tools = _mapping(report.get("tools"))
        sandbox = _mapping(tools.get("sandbox"))
        totals["sandbox_runs"] += _int(sandbox.get("runs"))
        totals["sandbox_failed"] += _int(sandbox.get("failed"))
        for tool, row in _mapping(tools.get("by_tool")).items():
            calls = _int(_mapping(row).get("calls"))
            tools_by_tool[str(tool)] = tools_by_tool.get(str(tool), 0) + calls
        failures = _mapping(report.get("failures"))
        totals["steps_failed"] += _int(failures.get("steps_failed"))
        for code, row in _mapping(failures.get("by_code")).items():
            count = _int(_mapping(row).get("count"))
            failures_by_code[str(code)] = failures_by_code.get(str(code), 0) + count
        iterations = _mapping(report.get("iterations"))
        totals["redo_total"] += _int(iterations.get("redo_total"))
        totals["auto_redo"] += _int(iterations.get("auto"))
        totals["retries"] += _int(iterations.get("retries"))
        totals["revisions"] += _int(iterations.get("revisions"))
        gates = _mapping(report.get("gates"))
        totals["gates_requested"] += _int(gates.get("requested"))
        totals["rollbacks"] += _int(gates.get("rollbacks"))
        recommended_followed += _int(gates.get("recommended_followed"))
        recommended_available += _int(gates.get("recommended_available"))
        reviews = _mapping(report.get("reviews"))
        totals["review_loops"] += _int(reviews.get("loops"))
        totals["review_executed"] += _int(reviews.get("executed"))
        totals["review_first_round_accept"] += _int(reviews.get("first_round_accept"))
        totals["review_stalemates"] += _int(reviews.get("stalemates"))
        audit = _mapping(report.get("audit"))
        if audit.get("available"):
            audits += 1
            audits_clean += int(_int(audit.get("findings_total")) == 0)
        totals["audit_findings_total"] += _int(audit.get("findings_total"))
        for row in _mapping(report.get("stages")).values():
            totals["quality_warnings"] += _int(_mapping(row).get("quality_warnings"))
    totals["cost_usd"] = round(sum(costs), 6) if costs else None
    totals["duration_s"] = round(sum(durations), 3) if durations else None
    runs = len(reports)
    return {
        "version": REPORT_VERSION,
        "runs": runs,
        "outcomes": outcomes,
        "completion_rate": _rate(outcomes["completed"], runs),
        "totals": totals,
        "rates": {
            "first_round_accept_rate": _rate(
                totals["review_first_round_accept"], totals["review_executed"]
            ),
            "stalemate_rate": _rate(totals["review_stalemates"], totals["review_executed"]),
            "recommended_followed_rate": _rate(recommended_followed, recommended_available),
            "violation_rate": _rate(repair_calls, started),
            "repair_success_rate": _rate(max(repair_calls - schema_failures, 0), repair_calls),
            "sandbox_failure_rate": _rate(totals["sandbox_failed"], totals["sandbox_runs"]),
            "audit_clean_rate": _rate(audits_clean, audits),
        },
        "failures_by_code": _sorted_dict(failures_by_code),
        "tools_by_tool": _sorted_dict(tools_by_tool),
        "means": {
            "duration_s": _mean(durations),
            "total_tokens": _mean(tokens),
            "llm_calls": _mean(llm_calls),
            "cost_usd": _mean(costs),
        },
    }


def _numeric_leaves(data: Any, prefix: str = "") -> dict[str, float]:
    """报告里所有数值叶子（int / float，不含 bool）→ ``{"a.b.c": value}``。"""
    leaves: dict[str, float] = {}
    if isinstance(data, Mapping):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path == "version":
                continue
            leaves.update(_numeric_leaves(value, path))
    elif isinstance(data, bool):
        return leaves
    elif isinstance(data, (int, float)):
        leaves[prefix] = data
    return leaves


def compare_reports(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """两份报告（运行级或批次级）的数值漂移：每个数值叶子给 baseline / current / delta / ratio
    （ratio = delta / baseline，基线为 0 或缺键 → None）；只列有变化的项，按路径排序。
    「tokens / 时长 / 费用批次漂移」（§14.2）就是把两批的 ``aggregate_batch`` 喂进来。"""
    before = _numeric_leaves(baseline)
    after = _numeric_leaves(current)
    entries: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path, 0)
        new = after.get(path, 0)
        if old == new:
            continue
        delta = new - old
        entries.append({
            "path": path,
            "baseline": old,
            "current": new,
            "delta": round(delta, 6) if isinstance(delta, float) else delta,
            "ratio": round(delta / old, 6) if old else None,
        })
    return {"changed": len(entries), "entries": entries}


# -- Markdown 渲染 --------------------------------------------------------------------------------


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return f"{text or '0'}{unit}"
    return f"{value}{unit}"


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_fmt(cell) for cell in row) + " |" for row in rows]
    return lines


def _rows(data: Mapping[str, Any], *keys: str) -> list[list[Any]]:
    """``{名: {键: 值}}`` → 表格行 ``[名, 值...]``（非字典的值跳过）。"""
    return [
        [name, *(row.get(key) for key in keys)]
        for name, row in data.items()
        if isinstance(row, Mapping)
    ]


def _bullet(*parts: str) -> str:
    return "- " + " · ".join(part for part in parts if part)


def _md_run(run: Mapping[str, Any]) -> list[str]:
    lines = [
        _bullet(
            f"结果：{run.get('outcome')}",
            f"最终状态 {run.get('final_state') or '—'}",
            f"图 {run.get('graph') or '—'}",
            f"事件 {run.get('events')}",
            f"时长 {_fmt(run.get('duration_s'), ' s')}",
        )
    ]
    if run.get("failed_state"):
        lines.append(f"- 失败阶段：{run['failed_state']}")
    return lines


def _md_stages(stages: Mapping[str, Any]) -> list[str]:
    return _table(
        ["阶段", "尝试", "成功", "失败", "LLM 调用", "沙盒轮", "波", "审稿轮", "质量告警"],
        _rows(
            stages, "attempts", "succeeded", "failed", "llm_attempts", "code_rounds", "waves",
            "review_rounds", "quality_warnings",
        ),
    )


def _md_gates(gates: Mapping[str, Any]) -> list[str]:
    lines = [
        _bullet(
            f"提出 {gates.get('requested')}",
            f"拍板 {gates.get('resolved')}",
            f"批准 {gates.get('approved')}",
            f"拒绝 {gates.get('rejected')}",
            f"回退 {gates.get('rollbacks')}",
            f"采纳推荐 {gates.get('recommended_followed')}/{gates.get('recommended_available')}",
            f"等待合计 {_fmt(gates.get('wait_s'), ' s')}",
        )
    ]
    by_gate = _mapping(gates.get("by_gate"))
    if by_gate:
        rows = [
            [gate, row.get("requested"), row.get("approved"), row.get("rejected"),
             row.get("rollbacks"),
             f"{row.get('recommended_followed')}/{row.get('recommended_available')}"]
            for gate, row in by_gate.items()
            if isinstance(row, Mapping)
        ]
        lines += ["", *_table(["闸门", "提出", "批准", "拒绝", "回退", "采纳推荐"], rows)]
    return lines


def _md_iterations(iterations: Mapping[str, Any]) -> list[str]:
    lines = [
        _bullet(
            f"回退 {iterations.get('redo_total')}"
            f"（自动 {iterations.get('auto')} / 人工 {iterations.get('manual')}）",
            f"闸门回退 {iterations.get('rollbacks')}",
            f"轮次用尽开门 {iterations.get('exhausted')}",
            f"修订 {iterations.get('revisions')}"
            f"（最高第 {iterations.get('revision_round_max')} 轮）",
            f"重试 {iterations.get('retries')}",
        )
    ]
    by_edge = _mapping(iterations.get("by_edge"))
    if by_edge:
        edges = "；".join(f"`{edge}` × {count}" for edge, count in by_edge.items())
        lines.append(f"- 按边：{edges}")
    return lines


def _md_failures(failures: Mapping[str, Any]) -> list[str]:
    if not failures.get("steps_failed") and not failures.get("run_failed"):
        return ["无失败。"]
    lines = [
        _bullet(
            f"步骤失败 {failures.get('steps_failed')}",
            f"运行失败 {failures.get('run_failed')}",
            f"取消 {failures.get('cancelled')}",
            f"进程中断 {failures.get('interrupted')}",
            f"无错误码 {failures.get('uncoded')}",
        )
    ]
    by_code = _mapping(failures.get("by_code"))
    if by_code:
        headers = ["错误码", "次数", "归属", "处置", "含义"]
        lines += ["", *_table(headers, _rows(by_code, "count", "owner", "disposition", "summary"))]
    by_state = _mapping(failures.get("by_state"))
    if by_state:
        lines.append("- 按阶段：" + "；".join(f"{s} × {n}" for s, n in by_state.items()))
    last_error = failures.get("last_error")
    if last_error:
        lines.append(f"- 最近错误：`{str(last_error).splitlines()[0][:160]}`")
    return lines


def _md_tools(tools: Mapping[str, Any]) -> list[str]:
    sandbox = _mapping(tools.get("sandbox"))
    knowledge = _mapping(tools.get("knowledge"))
    lines = [
        _bullet(
            f"工具调用 {tools.get('calls')}（失败 {tools.get('failed')}）",
            f"沙盒运行 {sandbox.get('runs')}（失败 {sandbox.get('failed')}）",
            f"知识库检索 {knowledge.get('calls')}",
        )
    ]
    by_tool = _mapping(tools.get("by_tool"))
    if by_tool:
        headers = ["工具", "调用", "失败", "累计时长 ms"]
        lines += ["", *_table(headers, _rows(by_tool, "calls", "failed", "duration_ms"))]
    subagents = _mapping(tools.get("subagents"))
    if subagents:
        headers = ["子代理", "派发", "done", "failed", "exhausted", "timeout"]
        rows = _rows(subagents, "spawned", "done", "failed", "exhausted", "timeout")
        lines += ["", *_table(headers, rows)]
    return lines


def _md_reviews(reviews: Mapping[str, Any]) -> list[str]:
    lines = [
        _bullet(
            f"审稿环 {reviews.get('loops')}",
            f"执行 {reviews.get('executed')}",
            f"一轮接受 {reviews.get('first_round_accept')}",
            f"驳回轮 {reviews.get('reject_rounds')}（静态 {reviews.get('static_reject_rounds')}）",
            f"僵持 {reviews.get('stalemates')}",
            f"阻断意见 {reviews.get('blockers')}",
            f"意见 {reviews.get('findings')}",
            f"复跑不一致 {reviews.get('rerun_inconsistent')}",
        )
    ]
    by_stage = _mapping(reviews.get("by_stage"))
    if by_stage:
        headers = ["阶段", "环", "执行", "一轮接受", "驳回轮", "僵持"]
        rows = _rows(
            by_stage, "loops", "executed", "first_round_accept", "reject_rounds", "stalemates"
        )
        lines += ["", *_table(headers, rows)]
    return lines


def _md_quality(robustness: Mapping[str, Any], audit: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if robustness.get("executed"):
        total = _int(robustness.get("checks_total"))
        passed = total - _int(robustness.get("checks_failed"))
        parts = [
            f"稳健性：{robustness.get('status')}",
            f"通过 {passed}/{total}（{_pct(robustness.get('pass_rate'))}）",
            f"检验 {robustness.get('rounds')} 趟",
            f"未覆盖须检验假设 {robustness.get('uncovered_focus')}",
        ]
        comparison = _mapping(robustness.get("round_comparison"))
        if comparison:
            parts.append(
                f"较上一轮：转为通过 {comparison.get('resolved')}"
                f" / 仍未通过 {comparison.get('still_failing')}"
                f" / 未复检 {comparison.get('not_rechecked')}"
            )
        lines.append(_bullet(*parts))
    else:
        lines.append("- 稳健性：复跑未执行")
    if audit.get("available"):
        by_kind = _mapping(audit.get("by_kind"))
        kinds = "、".join(f"{kind} {count}" for kind, count in by_kind.items()) or "无"
        lines.append(
            _bullet(
                f"终稿审计：发现 {audit.get('findings_total')}（{kinds}）",
                f"冻结数字 {audit.get('frozen_numbers')}",
                f"图件 {audit.get('figures_inserted')}/{audit.get('figures_total')} 已插入",
                f"文献 {audit.get('references_cited')}/{audit.get('references_total')} 已引用"
                f"，{audit.get('references_verified')} 已验证",
            )
        )
    else:
        lines.append("- 终稿审计：无论文步骤")
    return lines


def _md_llm(llm: Mapping[str, Any], process: Mapping[str, Any]) -> list[str]:
    if not llm.get("available"):
        lines = ["（无过程事件：评测会话或未提供 run.log，不估算）"]
    else:
        unpriced = llm.get("unpriced_models") or []
        cost = _fmt(llm.get("cost_usd"), " USD")
        if unpriced:
            cost += f"（未定价：{', '.join(unpriced)}）"
        repair_rate = _pct(llm.get("repair_success_rate"))
        lines = [
            _bullet(
                f"发起 {llm.get('started')}",
                f"成功 {llm.get('calls')}",
                f"传输失败 {llm.get('failed_calls')}",
                f"结构修复 {llm.get('repair_calls')}",
                f"违约率 {_pct(llm.get('violation_rate'))}",
                f"修复成功率 {repair_rate}（E120 {llm.get('schema_failures')}）",
            ),
            _bullet(
                f"tokens {llm.get('total_tokens')}（prompt {llm.get('prompt_tokens')}"
                f" / completion {llm.get('completion_tokens')}）",
                f"时长 {llm.get('elapsed_ms')} ms",
                f"费用 {cost}",
                f"估算 tokens {llm.get('estimated_token_calls')} 次",
                f"思考通道取答 {llm.get('answers_from_reasoning')} 次",
            ),
            "",
            *_table(
                ["微技能", "调用", "修复", "失败", "prompt", "completion", "ms"],
                _rows(
                    _mapping(llm.get("by_prompt")), "calls", "repair_calls", "failed",
                    "prompt_tokens", "completion_tokens", "elapsed_ms",
                ),
            ),
            "",
            *_table(
                ["型号", "调用", "prompt", "completion"],
                _rows(_mapping(llm.get("by_model")), "calls", "prompt_tokens", "completion_tokens"),
            ),
        ]
    if process.get("events"):
        lines += [
            "",
            _bullet(
                f"过程事件 {process.get('events')}",
                f"预算硬限 {process.get('budget_limit')}",
                f"自动回退用尽 {process.get('auto_redo_exhausted')}",
                f"进程重启 {process.get('executor_restarted')}",
            ),
        ]
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    """运行级 E6 看板（Markdown）：给 evals 记录与人工验收看的那一页。"""
    run = _mapping(report.get("run"))

    def part(key: str) -> Mapping[str, Any]:
        return _mapping(report.get(key))

    sections: list[tuple[str, list[str]]] = [
        ("阶段", _md_stages(part("stages"))),
        ("闸门", _md_gates(part("gates"))),
        ("回退与迭代", _md_iterations(part("iterations"))),
        ("失败与错误码", _md_failures(part("failures"))),
        ("工具与沙盒", _md_tools(part("tools"))),
        ("生成者-评审者", _md_reviews(part("reviews"))),
        ("稳健性与终稿审计", _md_quality(part("robustness"), part("audit"))),
        ("模型调用", _md_llm(part("llm"), part("process"))),
    ]
    lines = [f"# E6 看板 · {run.get('run_id') or '(unknown run)'}", "", *_md_run(run)]
    for title, body in sections:
        lines += ["", f"## {title}", "", *body]
    return "\n".join(lines) + "\n"


def render_batch_markdown(batch: Mapping[str, Any]) -> str:
    """批次级 E6 看板（Markdown）。"""
    outcomes = _mapping(batch.get("outcomes"))
    totals = _mapping(batch.get("totals"))
    rates = _mapping(batch.get("rates"))
    means = _mapping(batch.get("means"))
    lines = [
        "# E6 批次看板",
        "",
        f"- 运行数：{batch.get('runs')} · 完成率 {_pct(batch.get('completion_rate'))}",
        "- 结果分布：" + "；".join(f"{key} {value}" for key, value in outcomes.items()),
        "", "## 总量", "",
        *_table(["指标", "合计"], [[key, value] for key, value in totals.items()]),
        "", "## 比率（分子分母求和后重算）", "",
        *_table(["比率", "值"], [[key, _pct(value)] for key, value in rates.items()]),
        "", "## 均值（每运行）", "",
        *_table(["指标", "均值"], [[key, value] for key, value in means.items()]),
    ]
    by_code = _mapping(batch.get("failures_by_code"))
    if by_code:
        rows = [[code, count, *_catalog_entry(code).values()] for code, count in by_code.items()]
        lines += ["", "## 错误码", "", *_table(["错误码", "次数", "归属", "处置", "含义"], rows)]
    by_tool = _mapping(batch.get("tools_by_tool"))
    if by_tool:
        rows = [[tool, count] for tool, count in by_tool.items()]
        lines += ["", "## 工具调用", "", *_table(["工具", "调用"], rows)]
    return "\n".join(lines) + "\n"
