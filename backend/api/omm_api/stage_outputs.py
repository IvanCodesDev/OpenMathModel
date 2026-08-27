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

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_contracts import DatasetProfile, DeliveryManifest, DocumentDraft, ExperimentSummary, PlanProposal

from .api_models import StageOutputs
from .blobstore import ArtifactBlobStore, has_readable_local_content
from .orm import ArtifactRow, DomainEventRow, TaskRunRow
from .serialize import as_utc, iso_z

_PROBLEM_ANALYSIS = "PROBLEM_ANALYSIS"
_DATA_PREPARATION = "DATA_PREPARATION"
_MODEL_PLANNING = "MODEL_PLANNING"
_EXPERIMENTING = "EXPERIMENTING"
_VALIDATING = "VALIDATING"
_PAPER_WRITING = "PAPER_WRITING"

#: 契约 verdict 枚举（experiment-summary / delivery-manifest 共用 $defs）。
_VERDICTS = frozenset({"pass", "concerns", "fail"})


class StageState:
    """一个节点的「最新成功输出」：outputs 随后续 STEP_SUCCEEDED 覆盖，count 计成功次数。

    公开给 workspace_view 复用（执行计划的本题化文案同样以阶段最新输出为数据源）。
    """

    __slots__ = ("outputs", "at", "count")

    def __init__(self) -> None:
        self.outputs: dict[str, Any] = {}
        self.at: Optional[datetime] = None
        self.count = 0


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
            node = step_nodes.get(str(payload.get("step_id")))
            if node is None:
                continue
            state = stages.setdefault(node, StageState())
            state.outputs = dict(payload.get("outputs") or {})
            state.at = _dt(row.created_at)
            state.count += 1
    return stages, step_nodes


def _strs(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


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
        updated_at=iso_z(state.at),
    )


def _plan_proposal(run_id: str, state: Optional[StageState]) -> Optional[PlanProposal]:
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
            }
        )
    if not plans:
        # sim 节点或退化输出：契约要求 plans 至少一项（minItems=1），空表投影为 null
        return None
    return PlanProposal(
        run_id=run_id,
        plans=plans,
        recommended_plan_id=str(outputs.get("recommended_plan_id") or ""),
        rationale=outputs.get("rationale"),
        updated_at=iso_z(state.at),
    )


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
        updated_at=iso_z(updated_at),
    )


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
    artifact_rows = list(
        session.execute(
            select(ArtifactRow)
            .where(ArtifactRow.run_id == run.id)
            .order_by(ArtifactRow.created_at.asc(), ArtifactRow.id.asc())
        ).scalars()
    )

    return StageOutputs(
        run_id=run.id,
        dataset_profile=_dataset_profile(run.id, stages.get(_DATA_PREPARATION)),
        plan_proposal=_plan_proposal(run.id, stages.get(_MODEL_PLANNING)),
        experiment_summary=_experiment_summary(
            run.id, stages.get(_EXPERIMENTING), stages.get(_VALIDATING)
        ),
        document_draft=_document_draft(run.id, stages.get(_PAPER_WRITING)),
        delivery_manifest=_delivery_manifest(run, stages, step_nodes, artifact_rows, blobs),
    )
