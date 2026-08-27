"""把运行投影为 Web 建模流程可直接消费的语义视图。

这里集中维护 workflow node → 现有页面的唯一映射。API 不返回 HTML、CSS 类名或
DOM 选择器；Web 控制器把同一份语义状态同时渲染到 Agent 左栏与右侧既有模板。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from omm_contracts import ModelingWorkspaceView

from .blobstore import ArtifactBlobStore, has_readable_local_content
from .orm import (
    AgentEventRow,
    ApprovalRequestRow,
    ArtifactRow,
    ProjectRow,
    StepRunRow,
    TaskRunRow,
)
from .serialize import iso_z
from .stage_outputs import StageState, replay_stage_outputs
from .workflow import STAGE_LABELS

PAGE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "running",
        "label": "问题分析",
        "route": "/task/running",
        "nodes": ("CREATED", "PROBLEM_ANALYSIS"),
    },
    {
        "key": "data",
        "label": "数据准备",
        "route": "/workspace/data",
        "nodes": ("DATA_PREPARATION",),
    },
    {
        "key": "model",
        "label": "建模方案",
        "route": "/workspace/model-plan",
        "nodes": ("MODEL_PLANNING",),
    },
    {
        "key": "experiments",
        "label": "实验与验证",
        "route": "/workspace/experiments",
        "nodes": ("EXPERIMENTING", "VALIDATING"),
    },
    {
        "key": "editor",
        "label": "论文编辑",
        "route": "/workspace/paper-editor",
        "nodes": ("PAPER_WRITING",),
    },
    {
        "key": "complete",
        "label": "最终成果",
        "route": "/task/complete",
        "nodes": ("COMPLETED",),
    },
)

NODE_ORDER = {
    node: index
    for index, node in enumerate(
        (
            "CREATED",
            "PROBLEM_ANALYSIS",
            "DATA_PREPARATION",
            "MODEL_PLANNING",
            "EXPERIMENTING",
            "VALIDATING",
            "PAPER_WRITING",
            "COMPLETED",
        )
    )
}
PAGE_BY_NODE = {
    node: spec
    for spec in PAGE_SPECS
    for node in spec["nodes"]
}

STAGE_SUMMARIES = {
    "CREATED": "任务已经创建，正在准备读取题目、附件与运行参数。",
    "PROBLEM_ANALYSIS": "正在梳理目标、约束、子问题和交付要求，分析结果会同步到任务执行页。",
    "DATA_PREPARATION": "正在检查数据质量、字段含义与清洗状态，结果会同步到数据准备页。",
    "MODEL_PLANNING": "正在比较主方案、基线与风险条件，方案和审批状态会同步到建模方案页。",
    "EXPERIMENTING": "正在执行主方案与基线实验，运行产物会同步到实验结果页。",
    "VALIDATING": "正在核对指标、稳健性和复现记录，验证结果会同步到实验结果页。",
    "PAPER_WRITING": "正在整理可追溯的论文正文与引用，草稿产物会同步到论文编辑页。",
    "COMPLETED": "论文、图表、数据、代码与运行记录已经汇总到最终成果页。",
}


def _page_for_node(node: str) -> dict[str, Any]:
    return PAGE_BY_NODE.get(node, PAGE_SPECS[0])


#: plan_text 的展示上限（契约 maxLength 300 的保守余量）
_PLAN_TEXT_LIMIT = 240


def _clip_plan(text: str) -> str:
    return text if len(text) <= _PLAN_TEXT_LIMIT else text[: _PLAN_TEXT_LIMIT - 1] + "…"


def _chosen_plan(outputs: dict[str, Any]) -> dict[str, Any] | None:
    """与 agents/skills 的 chosen_plan 同语义：推荐方案优先，其次首个。"""
    plans = [plan for plan in outputs.get("plans") or [] if isinstance(plan, dict)]
    recommended = outputs.get("recommended_plan_id")
    for plan in plans:
        if plan.get("id") == recommended:
            return plan
    return plans[0] if plans else None


def _plan_texts(stages: dict[str, StageState]) -> dict[str, str]:
    """页面 key → 本任务专属的计划短句（执行计划面板的渐进细化数据源）。

    初稿来自问题分析的 plan_outline（按 stage 一条本题化短句）；建模方案产出后，
    「实验与验证」条目细化为选中方案的名称与步骤。状态不在这里派生——面板的
    勾选只信 pages.status（引擎执行事实），计划文本只负责「说人话」。
    没有 plan_outline（模拟链/旧运行/模型未给）时返回空表，展示层回退固定 label。
    """
    analysis = stages.get("PROBLEM_ANALYSIS")
    if analysis is None:
        return {}
    outline: dict[str, str] = {}
    for item in analysis.outputs.get("plan_outline") or []:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "")
        text = str(item.get("text") or "").strip()
        if stage and text:
            outline[stage] = text
    if not outline:
        return {}

    texts: dict[str, str] = {}
    for spec in PAGE_SPECS:
        parts = [outline[node] for node in spec["nodes"] if node in outline]
        if parts:
            texts[spec["key"]] = _clip_plan("；".join(parts))

    planning = stages.get("MODEL_PLANNING")
    if planning is not None:
        chosen = _chosen_plan(planning.outputs)
        if chosen is not None:
            name = str(chosen.get("name") or chosen.get("id") or "").strip()
            steps = [str(step).strip() for step in chosen.get("steps") or [] if str(step).strip()]
            lead = f"按方案「{name}」实施" if name else "按选定方案实施"
            detail = "：" + "；".join(steps[:3]) if steps else ""
            texts["experiments"] = _clip_plan(lead + detail)
    return texts


def _latest_steps(rows: Iterable[StepRunRow]) -> dict[str, StepRunRow]:
    latest: dict[str, StepRunRow] = {}
    for row in rows:
        current = latest.get(row.node)
        if current is None or (row.attempt, row.created_at, row.id) > (
            current.attempt,
            current.created_at,
            current.id,
        ):
            latest[row.node] = row
    return latest


def _page_status(
    run: TaskRunRow,
    nodes: tuple[str, ...],
    latest_steps: dict[str, StepRunRow],
) -> str:
    current = run.current_node or "CREATED"
    if current in nodes:
        if run.status == "QUEUED":
            return "PENDING"
        if run.status == "WAITING_APPROVAL":
            return "WAITING_APPROVAL"
        if run.status in {"PAUSED", "FAILED", "CANCELLED"}:
            return run.status
        if run.status == "COMPLETED" or current == "COMPLETED":
            return "SUCCEEDED"
        return "RUNNING"

    attempts = [latest_steps[node] for node in nodes if node in latest_steps]
    if any(step.status == "FAILED" for step in attempts):
        return "FAILED"
    if any(step.status == "RUNNING" for step in attempts):
        return "RUNNING"

    current_rank = NODE_ORDER.get(current)
    page_ranks = [NODE_ORDER[node] for node in nodes if node in NODE_ORDER]
    if current_rank is not None and page_ranks:
        if current_rank > max(page_ranks):
            return "SUCCEEDED"
        if current_rank < min(page_ranks):
            return "PENDING"
    if attempts and all(step.status in {"SUCCEEDED", "SKIPPED"} for step in attempts):
        return "SUCCEEDED"
    return "PENDING"


def _approval_projection(row: ApprovalRequestRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "title": row.title,
        "description": None,
        "options": list(row.options or []),
    }


def _preferred_option(approval: ApprovalRequestRow | None) -> str | None:
    if approval is None:
        return None
    options = list(approval.options or [])
    selectable = [option for option in options if option.get("id") != "reject"]
    return str(selectable[0]["id"]) if len(selectable) == 1 else None


def _agent_projection(
    run: TaskRunRow,
    active_page: dict[str, Any],
    approval: ApprovalRequestRow | None,
) -> dict[str, Any]:
    node = run.current_node or "CREATED"
    label = STAGE_LABELS.get(node, "任务准备" if node == "CREATED" else node)
    summary = STAGE_SUMMARIES.get(node, f"Agent 正在处理阶段 {node}，页面会持续同步最新状态。")
    state = "WORKING"
    action: dict[str, Any] = {
        "kind": "navigate",
        "label": f"查看{active_page['label']}",
        "target_route": active_page["route"],
        "approval_id": None,
        "option_id": None,
    }

    if run.status == "WAITING_APPROVAL" and approval is not None:
        state = "WAITING_APPROVAL"
        label = approval.title
        summary = f"{approval.title}。确认后，Agent 将从当前检查点继续执行。"
        action = {
            "kind": "approve",
            "label": "确认并继续",
            "target_route": active_page["route"],
            "approval_id": approval.id,
            "option_id": _preferred_option(approval),
        }
    elif run.status == "PAUSED":
        state = "PAUSED"
        summary = "任务已保留当前检查点，恢复后将从该阶段继续。"
        action = {
            "kind": "resume",
            "label": "恢复任务",
            "target_route": active_page["route"],
            "approval_id": None,
            "option_id": None,
        }
    elif run.status == "FAILED":
        state = "FAILED"
        label = f"{label}执行失败"
        summary = run.failure_message or "本阶段执行失败，可在保留已有产物的前提下重试。"
        action = {
            "kind": "retry",
            "label": "重试当前阶段",
            "target_route": active_page["route"],
            "approval_id": None,
            "option_id": None,
        }
    elif run.status == "COMPLETED":
        state = "COMPLETED"
        label = "全部成果已交付"
        summary = STAGE_SUMMARIES["COMPLETED"]
        action = {
            "kind": "navigate",
            "label": "继续优化论文",
            "target_route": "/workspace/paper-editor",
            "approval_id": None,
            "option_id": None,
        }
    elif run.status == "CANCELLED":
        state = "CANCELLED"
        label = "任务已取消"
        summary = "当前运行已经结束，已生成的历史记录和产物仍可查看。"
        action = {
            "kind": "none",
            "label": "任务已结束",
            "target_route": None,
            "approval_id": None,
            "option_id": None,
        }
    elif run.status == "QUEUED":
        state = "QUEUED"
        label = "任务正在排队"
        summary = "运行已进入队列，开始后会自动同步阶段与页面状态。"

    current_step = label if state in {"COMPLETED", "FAILED", "CANCELLED"} else f"正在{label}"
    if state == "WAITING_APPROVAL":
        current_step = f"等待确认：{label}"
    elif state == "PAUSED":
        current_step = f"已暂停：{label}"

    return {
        "state": state,
        "title": label,
        "summary": summary,
        "current_step": current_step,
        "action": action,
    }


def build_modeling_workspace_view(
    session: Session,
    run: TaskRunRow,
    blobs: ArtifactBlobStore,
) -> ModelingWorkspaceView:
    project = session.get(ProjectRow, run.project_id)
    steps = list(
        session.execute(
            select(StepRunRow)
            .where(StepRunRow.run_id == run.id)
            .order_by(StepRunRow.created_at.asc(), StepRunRow.id.asc())
        ).scalars()
    )
    latest_steps = _latest_steps(steps)
    step_nodes = {step.id: step.node for step in steps}
    # 项目级上传（run_id 为空）= 用户随任务提交的附件：当前产品流程一个任务
    # 对应一个项目，它们属于本次运行的输入，顶栏附件与对话上下文都要能看到。
    artifact_rows = list(
        session.execute(
            select(ArtifactRow)
            .where(
                ArtifactRow.project_id == run.project_id,
                or_(ArtifactRow.run_id == run.id, ArtifactRow.run_id.is_(None)),
            )
            .order_by(ArtifactRow.created_at.asc(), ArtifactRow.id.asc())
        ).scalars()
    )
    artifacts: list[dict[str, Any]] = []
    artifact_nodes: dict[str, str | None] = {}
    for row in artifact_rows:
        producer_node = step_nodes.get(row.producer_step or "")
        artifact_nodes[row.id] = producer_node
        downloadable = row.status == "READY" and has_readable_local_content(
            blobs, row.uri, row.sha256
        )
        artifacts.append(
            {
                "id": row.id,
                "kind": row.kind,
                "name": row.name,
                "media_type": row.media_type or "application/octet-stream",
                "size_bytes": row.size_bytes,
                "status": row.status,
                "producer_node": producer_node,
                "download_url": f"/api/v1/artifacts/{row.id}/download" if downloadable else None,
            }
        )

    approval = session.execute(
        select(ApprovalRequestRow)
        .where(
            ApprovalRequestRow.run_id == run.id,
            ApprovalRequestRow.status == "PENDING",
        )
        .order_by(ApprovalRequestRow.requested_at.desc())
    ).scalars().first()
    latest_event = session.execute(
        select(AgentEventRow)
        .where(AgentEventRow.run_id == run.id)
        .order_by(AgentEventRow.sequence.desc())
    ).scalars().first()
    latest_sequence = latest_event.sequence if latest_event is not None else None

    active_node = run.current_node or "CREATED"
    active_page = _page_for_node(active_node)
    projection_times = [run.updated_at]
    if latest_event is not None:
        projection_times.append(latest_event.created_at)
    if approval is not None:
        projection_times.append(approval.requested_at)
    projection_times.extend(row.created_at for row in artifact_rows)
    stage_states, _ = replay_stage_outputs(session, run.id)
    plan_texts = _plan_texts(stage_states)
    pages = []
    for spec in PAGE_SPECS:
        nodes = tuple(spec["nodes"])
        pages.append(
            {
                **spec,
                "nodes": list(nodes),
                "status": _page_status(run, nodes, latest_steps),
                "plan_text": plan_texts.get(spec["key"]),
                "artifact_ids": list(artifact_nodes)
                if spec["key"] == "complete"
                else [
                    artifact_id
                    for artifact_id, producer_node in artifact_nodes.items()
                    if producer_node in nodes
                ],
            }
        )

    return ModelingWorkspaceView(
        run_id=run.id,
        project_id=run.project_id,
        project_name=project.name if project is not None else "未命名项目",
        goal=run.goal,
        workflow_version=run.workflow_version,
        run_status=run.status,
        active_node=active_node,
        active_page=active_page["key"],
        suggested_route=active_page["route"],
        agent=_agent_projection(run, active_page, approval),
        pages=pages,
        artifacts=artifacts,
        pending_approval=_approval_projection(approval),
        latest_event_sequence=latest_sequence,
        updated_at=iso_z(max(projection_times)),
    )
