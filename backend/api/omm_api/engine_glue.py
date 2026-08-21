"""agents/core 引擎 ←→ backend/api 的胶水层（B2 换脑）。

架构（主规划 §5.3 / §13 Phase 2「状态投影器」）：
- ``run_domain_events`` 表是执行事实来源：引擎事件先落表（同事务），再驱动 v1 投影；
- v1 行（task_runs/step_runs/artifacts/approval_requests/agent_events）是投影，
  对外契约与事件流保持与旧模拟推进器兼容；
- 审批行的"解决"侧（option/comment/client_token 与 v1 approval.resolved 事件）归
  动作层 actions.py（它持有这些上下文），投影只负责审批行创建与请求/状态事件；
- 节点装配按运行归属决定：run → project.owner → users.llm_config。配置了
  自定义 API 的用户，问题分析与建模方案两个阶段走 agents/skills 的真实 LLM
  节点（llm.EngineLlmPort 出网）；未配置或提示词缺失时整条链回落 sim-0.1
  模拟节点，其余阶段（数据准备/实验/检验/论文）在真实节点补齐前仍为模拟。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_agent_core import (
    AgentEvent as CoreEvent,
)
from omm_agent_core import (
    ArtifactRef,
    EventType,
    NodeContext,
    NodeResult,
    NodeServices,
    TaskRunEngine,
    TaskRunSnapshot,
    TaskState,
    replay_events,
)
from omm_agent_skills import (
    ModelPlanningNode,
    ProblemAnalysisNode,
    PromptRegistry,
    load_default_registry,
)
from omm_contracts import (
    AgentEventType,
    ApprovalDecisionType,
    ApprovalStatus,
    ArtifactStatus,
    StepRunStatus,
    TaskRunStatus,
)

from .blobstore import ArtifactBlobStore, LocalContentStore
from .config import get_settings
from .events import append_event
from .ids import new_id
from .llm import EngineLlmPort, config_usable, is_third_party_host, parse_llm_config
from .models import User
from .orm import (
    ApprovalRequestRow,
    ArtifactRow,
    DomainEventRow,
    ProjectRow,
    StepRunRow,
    TaskRunRow,
)
from .serialize import utcnow
from .usage import budget_exhausted, is_free_endpoint, record_usage
from .workflow import NODE_COMPLETED, STAGE_LABELS

logger = logging.getLogger("omm.engine")

CANCELLED_ERROR = "cancelled by user"
REJECT_OPTION_ID = "reject"

_ARTIFACT_NAMES = {
    "figure": "基线实验结果图（模拟）",
    "report": "建模报告草稿（模拟）",
}

FAIL_EXPERIMENT_MARKER = "[fail:experiment]"


# ── 时钟与 ID：注入 API 的约定（32 位 hex 前缀 ID 满足 v1 模式） ────────────


class _ApiClock:
    def now_iso(self) -> str:
        return utcnow().isoformat()


class _ApiIds:
    def new_id(self, prefix: str) -> str:
        return new_id(prefix)


# ── Artifact 存储端口：进程级绑定（create_app 时注入；缺省按配置构建） ──────

_blobstore: ArtifactBlobStore | None = None


def set_blobstore(store: ArtifactBlobStore) -> None:
    global _blobstore
    _blobstore = store


def get_blobstore() -> ArtifactBlobStore:
    global _blobstore
    if _blobstore is None:
        _blobstore = LocalContentStore(get_settings().artifacts_dir)
    return _blobstore


class ApiArtifactStore:
    """实现 omm_agent_core 的 ArtifactStore 端口：内容落 BlobStore，引用带 local:// URI。"""

    def __init__(self, blobs: ArtifactBlobStore) -> None:
        self._blobs = blobs

    def put(
        self,
        run_id: str,
        kind: str,
        name: str,
        content: bytes,
        media_type: str,
        producer_step: str,
    ) -> ArtifactRef:
        sha256, size = self._blobs.put(content)
        return ArtifactRef(
            artifact_id=new_id("art"),
            kind=kind,
            uri=f"local://{sha256}/{name}",
            sha256=sha256,
            size=size,
            media_type=media_type,
            producer_step=producer_step,
        )


# ── sim-0.1 节点：与旧模拟推进器行为等价 ──────────────────────────────────


def _fail_config(inputs: dict[str, Any]) -> tuple[Optional[str], int]:
    params = dict(inputs.get("params") or {})
    fail_at = params.get("fail_at")
    fail_attempts = int(params.get("fail_attempts") or 1)
    if not fail_at and FAIL_EXPERIMENT_MARKER in str(inputs.get("goal") or ""):
        fail_at, fail_attempts = "EXPERIMENTING", 1
    return fail_at, fail_attempts


class SimStageNode:
    """一个工作状态一个节点；失败注入与产物行为对齐旧推进器。产物经 ArtifactStore 端口真实落盘。"""

    def __init__(self, state: TaskState) -> None:
        self._state = state

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        fail_at, fail_attempts = _fail_config(dict(ctx.inputs))
        if fail_at == self._state.value and ctx.attempt <= fail_attempts:
            return NodeResult.failed("失败注入：实验代码在本次尝试中报错（模拟）")

        artifacts: tuple[ArtifactRef, ...] = ()
        if self._state in (TaskState.EXPERIMENTING, TaskState.PAPER_WRITING):
            if services.artifacts is None:
                return NodeResult.failed("artifact 存储端口未装配，无法发布产物")
            if self._state is TaskState.EXPERIMENTING:
                kind, media, filename = "figure", "image/svg+xml", "baseline-metrics.svg"
                content = f"<svg><!-- simulated baseline figure run={ctx.run_id} attempt={ctx.attempt} --></svg>"
            else:
                kind, media, filename = "report", "text/markdown", "report-draft.md"
                content = f"# 建模报告草稿（模拟）\n\nrun: {ctx.run_id}\n"
            artifacts = (
                services.artifacts.put(
                    ctx.run_id,
                    kind,
                    filename,
                    content.encode("utf-8"),
                    media,
                    ctx.step_id,
                ),
            )

        label = STAGE_LABELS.get(self._state.value, self._state.value)
        if self._state is TaskState.MODEL_PLANNING:
            return NodeResult.needs_review(
                "确认建模方案后继续实验", outputs={"label": label}
            )
        return NodeResult.succeeded(outputs={"label": label}, artifacts=artifacts)


SIM_NODES = {state: SimStageNode(state) for state in TaskState if state.name in {
    "PROBLEM_ANALYSIS",
    "DATA_PREPARATION",
    "MODEL_PLANNING",
    "EXPERIMENTING",
    "VALIDATING",
    "PAPER_WRITING",
}}


# ── 真实 LLM 节点（设置中心「自定义 API」已配置时启用） ────────────────────


#: 附件摘要总量与单附件正文上限：控制进入提示词的 token 规模
_ATTACHMENT_SUMMARY_LIMIT = 4000
_ATTACHMENT_EXCERPT_LIMIT = 1200


def _attachments_summary(params: dict[str, Any]) -> str:
    """运行参数里的附件元数据（含前端解析的正文摘要）→ 提示词附件摘要段。

    真实题面常在附件里而不在首句指令里；把 excerpt 交给问题分析节点，
    分析产出（含 title）才反映实际要解决的问题。
    """
    entries = params.get("attachment_metadata")
    if not isinstance(entries, list):
        return ""
    parts: list[str] = []
    used = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        excerpt = re.sub(r"\s+", " ", str(entry.get("excerpt") or "")).strip()
        piece = (
            f"《{name}》：{excerpt[:_ATTACHMENT_EXCERPT_LIMIT]}"
            if excerpt
            else f"《{name}》（正文尚未提取，仅有文件元数据）"
        )
        if used + len(piece) > _ATTACHMENT_SUMMARY_LIMIT:
            parts.append("（其余附件从略）")
            break
        parts.append(piece)
        used += len(piece)
    return "\n".join(parts)


class _GoalProblemAnalysisNode(ProblemAnalysisNode):
    """把运行输入的 goal 映射成提示词需要的 problem_statement。"""

    def build_variables(self, ctx: Any) -> dict[str, Any]:
        statement = ctx.inputs.get("problem_statement") or ctx.inputs.get("goal") or ""
        summary = str(ctx.inputs.get("attachments_summary") or "").strip()
        if not summary:
            summary = _attachments_summary(dict(ctx.inputs.get("params") or {}))
        return {
            "problem_statement": str(statement),
            "attachments_summary": summary or "无",
        }


@lru_cache(maxsize=1)
def _prompt_registry() -> Optional[PromptRegistry]:
    """agents/prompts 的模板注册表；目录缺失（异常部署）时返回 None 并回落模拟。"""
    try:
        registry = load_default_registry()
    except Exception:  # noqa: BLE001 - 提示词损坏不允许拖垮控制面
        logger.exception("加载提示词模板失败，任务将回落模拟节点")
        return None
    required = {"problem_analysis.default", "model_planning.default"}
    if not required.issubset(set(registry.ids())):
        logger.warning("提示词模板不完整（%s），任务将回落模拟节点", registry.ids())
        return None
    return registry


def _llm_wiring(session: Session, run: TaskRunRow) -> tuple[Optional[EngineLlmPort], dict]:
    """按运行归属解析自定义 API 配置：可用则换上真实 LLM 节点。"""
    owner = session.execute(
        select(ProjectRow.owner).where(ProjectRow.id == run.project_id)
    ).scalar_one_or_none()
    user = session.get(User, owner) if owner else None
    if user is None:
        return None, {}
    config = parse_llm_config(user.llm_config)
    registry = _prompt_registry()
    if not config_usable(config) or registry is None:
        return None, {}
    # 预算硬限制（设置中心「用量监控」）：达标后只留本地/免费接口；
    # 一个不剩则整条链回落模拟节点，并在 run.log 留痕说明原因。
    if budget_exhausted(session, user):
        free = tuple(endpoint for endpoint in config.endpoints if is_free_endpoint(endpoint))
        if not free:
            append_event(
                session,
                run.id,
                AgentEventType.run_log.value,
                {
                    "kind": "budget_limit",
                    "message": "本月预估费用已达预算上限，付费模型已暂停，"
                    "本次任务改用模拟节点推进；可在设置中心「用量监控」调整预算或关闭硬限制",
                },
            )
            return None, {}
        config = replace(config, endpoints=free)
    # 模型调用的过程事件（思考内容/调用摘要）进 run.log：与本次 advance 同事务
    # 提交，SSE 把它们转发给工作台的执行轨迹逐条展示。每次成功调用同时记入
    # llm_usage_records（用量监控），与本次 advance 同事务。
    port = EngineLlmPort(
        config,
        registry,
        on_event=lambda payload: append_event(
            session, run.id, AgentEventType.run_log.value, payload
        ),
        on_usage=lambda outcome: record_usage(
            session,
            user_id=user.id,
            source="agent",
            outcome=outcome,
            third_party=is_third_party_host(outcome.endpoint.host),
            run_id=run.id,
        ),
    )
    overrides = {
        TaskState.PROBLEM_ANALYSIS: _GoalProblemAnalysisNode(registry),
        TaskState.MODEL_PLANNING: ModelPlanningNode(registry),
    }
    return port, overrides


# ── 领域事件 → v1 投影 ─────────────────────────────────────────────────────


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _project_status(
    session: Session, run: TaskRunRow, to_status: str, reason: str
) -> None:
    from_status = run.status
    run.status = to_status
    run.updated_at = utcnow()
    append_event(
        session,
        run.id,
        AgentEventType.run_status_changed.value,
        {"from": from_status, "to": to_status, "reason": reason},
    )


def _project_node(session: Session, run: TaskRunRow, to_node: str, reason: str) -> None:
    from_node = run.current_node
    run.current_node = to_node
    run.updated_at = utcnow()
    append_event(
        session,
        run.id,
        AgentEventType.run_node_changed.value,
        {
            "from": from_node,
            "to": to_node,
            "reason": reason,
            "label": STAGE_LABELS.get(to_node, to_node),
        },
    )


_GOAL_NAME_PREFIX = re.compile(r"^(?:请帮我|请|帮我|我想(?:要)?)")
_GOAL_NAME_SPLIT = re.compile(r"[。！？!?；;\n]")


def derive_name_from_goal(goal: str) -> str:
    """复刻 apps/web task-start-state.ts 的 deriveProjectName：识别「仍是自动名」。

    两端算法必须一致，才能判断项目名是否还是创建时从首句截取的默认值；
    不一致时的后果是安全的——只会跳过自动重命名，绝不覆盖用户手动改的名。
    """
    compact = re.sub(r"\s+", " ", goal.replace("\r\n", "\n").replace("\r", "\n")).strip()
    compact = _GOAL_NAME_PREFIX.sub("", compact).strip()
    first = _GOAL_NAME_SPLIT.split(compact, maxsplit=1)[0].strip() or "未命名建模任务"
    characters = list(first)
    return "".join(characters[:24]) + "…" if len(characters) > 24 else first


def _maybe_rename_project(session: Session, run: TaskRunRow, outputs: dict[str, Any]) -> None:
    """问题分析产出 title 后，把「最近任务」的名字换成实际讨论的问题。

    仅当项目名仍等于按 goal 自动截取的默认名时才替换：名字对不上说明用户
    已手动命名，视为显式意图不覆盖。sim 节点的 outputs 没有 title，自然跳过。
    """
    title = re.sub(r"\s+", " ", str(outputs.get("title") or "")).strip()[:60]
    if not title:
        return
    project = session.get(ProjectRow, run.project_id)
    if project is None or project.name == title:
        return
    if project.name != derive_name_from_goal(run.goal or ""):
        return
    previous = project.name
    project.name = title
    project.updated_at = utcnow()
    append_event(
        session,
        run.id,
        AgentEventType.run_log.value,
        {
            "kind": "task_renamed",
            "message": f"已按题意将任务命名为「{title}」",
            "from": previous,
            "to": title,
        },
    )


def _input_hash(run: TaskRunRow, node: str, attempt: int) -> str:
    canonical = json.dumps(
        {"run_id": run.id, "node": node, "attempt": attempt, "params": run.params or {}},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _project(session: Session, run: TaskRunRow, event: CoreEvent) -> None:
    """把一条领域事件翻译成 v1 行与 v1 事件（agent_events 自己维护 sequence）。"""
    kind = event.event_type
    payload = event.payload
    at = _dt(event.created_at)

    if kind is EventType.RUN_CREATED:
        inputs = dict(payload.get("inputs") or {})
        append_event(
            session,
            run.id,
            AgentEventType.run_created.value,
            {"goal": inputs.get("goal"), "auto_start": bool(inputs.get("auto_start", True))},
        )
        return

    if kind is EventType.STATE_CHANGED:
        to_node = str(payload["to"])
        if run.status == TaskRunStatus.QUEUED.value:
            run.started_at = at
            _project_status(session, run, TaskRunStatus.RUNNING.value, "任务开始")
        _project_node(session, run, to_node, "进入阶段")
        return

    if kind is EventType.STEP_STARTED:
        node = str(payload["state"])
        attempt = int(payload["attempt"])
        if run.current_node != node:
            _project_node(session, run, node, "阶段开始")
        session.add(
            StepRunRow(
                id=str(payload["step_id"]),
                run_id=run.id,
                node=node,
                attempt=attempt,
                status=StepRunStatus.RUNNING.value,
                input_hash=_input_hash(run, node, attempt),
                detail=f"{STAGE_LABELS.get(node, node)}（第 {attempt} 次尝试）",
                created_at=at,
                started_at=at,
            )
        )
        append_event(
            session,
            run.id,
            AgentEventType.step_started.value,
            {"node": node, "attempt": attempt, "label": STAGE_LABELS.get(node, node)},
        )
        return

    if kind is EventType.STEP_SUCCEEDED:
        step = session.get(StepRunRow, str(payload["step_id"]))
        if step is not None:
            step.status = StepRunStatus.SUCCEEDED.value
            step.ended_at = at
            append_event(
                session,
                run.id,
                AgentEventType.step_succeeded.value,
                {"node": step.node, "attempt": step.attempt},
            )
            if step.node == TaskState.PROBLEM_ANALYSIS.value:
                _maybe_rename_project(session, run, dict(payload.get("outputs") or {}))
        return

    if kind is EventType.STEP_FAILED:
        step = session.get(StepRunRow, str(payload["step_id"]))
        if step is not None:
            step.status = StepRunStatus.FAILED.value
            step.failure_class = "CODE_DEFECT"
            step.detail = str(payload.get("error") or "step failed")
            step.ended_at = at
            append_event(
                session,
                run.id,
                AgentEventType.step_failed.value,
                {"node": step.node, "attempt": step.attempt, "failure_class": "CODE_DEFECT"},
            )
        return

    if kind is EventType.ARTIFACT_PRODUCED:
        ref = dict(payload["artifact"])
        name = _ARTIFACT_NAMES.get(str(ref.get("kind")), str(ref.get("kind")))
        session.add(
            ArtifactRow(
                id=str(ref["artifact_id"]),
                project_id=run.project_id,
                run_id=run.id,
                kind=str(ref["kind"]),
                name=name,
                uri=str(ref["uri"]),
                sha256=str(ref["sha256"]),
                size_bytes=int(ref["size"]),
                media_type=str(ref["media_type"]),
                producer_step=str(ref["producer_step"]),
                status=ArtifactStatus.READY.value,
                created_at=at,
            )
        )
        append_event(
            session,
            run.id,
            AgentEventType.artifact_published.value,
            {
                "artifact_id": ref["artifact_id"],
                "kind": ref["kind"],
                "name": name,
                "uri": ref["uri"],
            },
        )
        return

    if kind is EventType.REVIEW_REQUESTED:
        approval = ApprovalRequestRow(
            id=new_id("appr"),
            run_id=run.id,
            decision_type=ApprovalDecisionType.confirm_plan.value,
            title=str(payload.get("reason") or "确认建模方案后继续实验"),
            options=[
                {"id": "approve", "label": "采用当前方案", "description": "确认方案并进入实验阶段"},
                {"id": "reject", "label": "退回重做方案", "description": "重新执行建模方案阶段并再次确认"},
            ],
            evidence={"note": "模拟工作流生成的方案确认请求（sim-0.1）"},
            status=ApprovalStatus.PENDING.value,
            requested_at=at,
        )
        session.add(approval)
        append_event(
            session,
            run.id,
            AgentEventType.approval_requested.value,
            {
                "approval_id": approval.id,
                "decision_type": approval.decision_type,
                "title": approval.title,
            },
        )
        _project_status(session, run, TaskRunStatus.WAITING_APPROVAL.value, "等待方案确认")
        return

    if kind is EventType.REVIEW_RESOLVED:
        # 审批行更新与 v1 approval.resolved 事件由动作层完成（它持有 option/comment）
        if payload.get("approved"):
            _project_status(session, run, TaskRunStatus.RUNNING.value, "方案已确认")
        return

    if kind is EventType.RUN_RETRIED:
        run.failure_class = None
        run.failure_message = None
        if run.status == TaskRunStatus.WAITING_APPROVAL.value:
            _project_status(session, run, TaskRunStatus.RUNNING.value, "方案退回重做")
        elif run.status != TaskRunStatus.RUNNING.value:
            _project_status(session, run, TaskRunStatus.RUNNING.value, "重试失败阶段")
        return

    if kind is EventType.RUN_PAUSED:
        run.paused_from_status = run.status
        _project_status(session, run, TaskRunStatus.PAUSED.value, "用户暂停")
        return

    if kind is EventType.RUN_RESUMED:
        run.paused_from_status = None
        _project_status(session, run, TaskRunStatus.RUNNING.value, "用户恢复")
        return

    if kind is EventType.RUN_CANCELLED:
        return  # 只是意图标志；终态由随后的 advance 落定

    if kind is EventType.RUN_COMPLETED:
        run.ended_at = at
        _project_node(session, run, NODE_COMPLETED, "全部阶段完成")
        _project_status(session, run, TaskRunStatus.COMPLETED.value, "全部阶段完成")
        return

    if kind is EventType.RUN_FAILED:
        error = str(payload.get("error") or "unknown failure")
        if error == CANCELLED_ERROR:
            now = utcnow()
            running_steps = session.execute(
                select(StepRunRow).where(
                    StepRunRow.run_id == run.id,
                    StepRunRow.status == StepRunStatus.RUNNING.value,
                )
            ).scalars()
            for step in running_steps:
                step.status = StepRunStatus.CANCELLED.value
                step.ended_at = now
            pending = session.execute(
                select(ApprovalRequestRow).where(
                    ApprovalRequestRow.run_id == run.id,
                    ApprovalRequestRow.status == ApprovalStatus.PENDING.value,
                )
            ).scalars()
            for approval in pending:
                approval.status = ApprovalStatus.CANCELLED.value
            run.paused_from_status = None
            run.ended_at = now
            _project_status(session, run, TaskRunStatus.CANCELLED.value, "用户取消")
            return
        run.failure_class = "CODE_DEFECT"
        run.failure_message = error
        _project_status(session, run, TaskRunStatus.FAILED.value, "实验步骤失败")
        return

    if kind is EventType.TOOL_CALLED:
        append_event(session, run.id, AgentEventType.run_log.value, dict(payload))
        return


class _ProjectingSink:
    """引擎事件汇：先落 run_domain_events（执行真相），再做 v1 投影。同一事务提交。"""

    def __init__(self, session: Session, run: TaskRunRow) -> None:
        self._session = session
        self._run = run

    def emit(self, event: CoreEvent) -> None:
        self._session.add(
            DomainEventRow(
                run_id=event.run_id,
                seq=event.seq,
                event_type=event.event_type.value,
                payload=event.payload,
                created_at=event.created_at,
            )
        )
        _project(self._session, self._run, event)


# ── 适配器：runner / actions / router 的统一入口 ──────────────────────────


def _load_core_events(session: Session, run_id: str) -> list[CoreEvent]:
    rows = session.execute(
        select(DomainEventRow)
        .where(DomainEventRow.run_id == run_id)
        .order_by(DomainEventRow.seq.asc())
    ).scalars()
    return [
        CoreEvent(
            run_id=row.run_id,
            seq=row.seq,
            event_type=EventType(row.event_type),
            payload=dict(row.payload or {}),
            created_at=row.created_at,
        )
        for row in rows
    ]


def _build_engine(session: Session, run: TaskRunRow) -> TaskRunEngine:
    llm_port, node_overrides = _llm_wiring(session, run)
    return TaskRunEngine(
        sink=_ProjectingSink(session, run),
        clock=_ApiClock(),
        ids=_ApiIds(),
        nodes={**SIM_NODES, **node_overrides},
        services=NodeServices(
            clock=_ApiClock(),
            ids=_ApiIds(),
            artifacts=ApiArtifactStore(get_blobstore()),
            llm=llm_port,
        ),
    )


def open_engine(
    session: Session, run: TaskRunRow
) -> tuple[TaskRunEngine, TaskRunSnapshot]:
    engine = _build_engine(session, run)
    events = _load_core_events(session, run.id)
    snapshot = replay_events(run.id, run.project_id, events)
    return engine, snapshot


def create_run_events(session: Session, run: TaskRunRow, goal: str, auto_start: bool) -> None:
    """为新建的 v1 运行行播种领域日志（RUN_CREATED → v1 run.created 投影）。"""
    engine = _build_engine(session, run)
    engine.create_run(
        project_id=run.project_id,
        inputs={"goal": goal, "params": run.params or {}, "auto_start": auto_start},
        run_id=run.id,
    )


def advance_run(session: Session, run: TaskRunRow) -> None:
    """一次 tick：终态/等待态直接返回，否则引擎推进一步（含取消落定）。"""
    idle = {
        TaskRunStatus.PAUSED.value,
        TaskRunStatus.WAITING_APPROVAL.value,
        TaskRunStatus.FAILED.value,
        TaskRunStatus.COMPLETED.value,
        TaskRunStatus.CANCELLED.value,
    }
    if run.status in idle:
        return
    engine, snapshot = open_engine(session, run)
    engine.advance(snapshot)


def pause_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.request_pause(snapshot)


def resume_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.resume(snapshot)


def cancel_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.request_cancel(snapshot)
    engine.advance(snapshot)  # 立即落定 CANCELLED（同步等价于 worker 的 finalize enqueue）


def retry_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.retry(snapshot)


def resolve_approval(session: Session, run: TaskRunRow, option_id: str) -> None:
    """approve 动作的引擎侧：批准=继续下一阶段；拒绝=重做 MODEL_PLANNING 并再次请求确认。"""
    engine, snapshot = open_engine(session, run)
    if option_id == REJECT_OPTION_ID:
        engine.resolve_review(snapshot, approved=False, reason="方案退回重做")
        engine.retry(snapshot)
        engine.advance(snapshot)  # 重跑 MODEL_PLANNING（attempt+1）并再次进入审批
        return
    engine.resolve_review(snapshot, approved=True, reason=option_id)
    engine.advance(snapshot)  # 启动下一阶段（EXPERIMENTING）
