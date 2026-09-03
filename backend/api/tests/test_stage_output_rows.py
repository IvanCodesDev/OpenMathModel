"""stage_outputs 版本化表（设计 §10.2 / D1.6，H1）：写侧落行与读侧覆盖。

写侧：STEP_SUCCEEDED 投影按节点落行，新版本 current、旧版本 superseded；
模拟节点（只有 {"label"}）与读侧空投影同一门槛，不落行。
读侧：build_stage_outputs 以表行覆盖事件重放（旧运行无行时重放兜底），
五类正文端点行为与之前一致（既有 test_stage_outputs 全量守护）。
"""

from __future__ import annotations

from conftest import (
    approve_when_asked,
    confirm_delivery,
    create_project,
    create_run,
    pending_approval,
    run_status_is,
    wait_until,
)
from sqlalchemy import select

from omm_api.orm import StageOutputRow
from test_task_runs_llm_nodes import PAPER_OUTLINE_OUTPUT, _configure_llm


def _rows(client, run_id: str) -> list[StageOutputRow]:
    with client.app.state.db.session_factory() as session:
        rows = list(
            session.execute(
                select(StageOutputRow)
                .where(StageOutputRow.run_id == run_id)
                .order_by(StageOutputRow.node.asc(), StageOutputRow.version.asc())
            ).scalars()
        )
        session.expunge_all()
    return rows


def test_real_chain_persists_versioned_rows_per_node(client, monkeypatch):
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")

    approve_when_asked(client, run["id"], option_id="approve")
    confirm_delivery(client, run["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    rows = _rows(client, run["id"])
    by_node = {row.node: row for row in rows}
    assert set(by_node) == {
        "PROBLEM_ANALYSIS", "DATA_PREPARATION", "MODEL_PLANNING",
        "EXPERIMENTING", "VALIDATING", "PAPER_WRITING",
    }, "六个真实节点各落一行"
    assert all(row.version == 1 and row.status == "current" for row in rows)
    assert by_node["MODEL_PLANNING"].schema_id == "model-planning.outputs.v1"
    assert by_node["PAPER_WRITING"].content["title"] == PAPER_OUTLINE_OUTPUT["title"]
    assert all(len(row.content_hash) == 64 for row in rows), "内容寻址哈希齐备"
    assert all(row.producer_step_id for row in rows), "行必须可回指产出步骤"

    # 读侧覆盖后端点行为不变：版本号与表行一致
    payload = client.get(f"/api/v1/task-runs/{run['id']}/stage-outputs").json()
    assert payload["document_draft"]["version"] == by_node["PAPER_WRITING"].version


def test_replan_supersedes_v1_and_writes_v2(client, monkeypatch):
    """H1 DoD：重做产生 v2 且 v1 superseded（审批拒绝 → 方案阶段重跑）。"""
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")

    wait_until(client, run["id"], pending_approval(client, run["id"]))
    approve_when_asked(client, run["id"], option_id="reject")
    # 拒绝触发方案阶段重跑并再次挂起审批
    wait_until(client, run["id"], pending_approval(client, run["id"]))

    planning_rows = [row for row in _rows(client, run["id"]) if row.node == "MODEL_PLANNING"]
    assert [(row.version, row.status) for row in planning_rows] == [
        (1, "superseded"),
        (2, "current"),
    ]
    # 两个版本都可回溯到各自的产出步骤（审计语义）
    assert planning_rows[0].producer_step_id != planning_rows[1].producer_step_id

    # 读侧以 current 行为准
    payload = client.get(f"/api/v1/task-runs/{run['id']}/stage-outputs").json()
    assert payload["plan_proposal"]["recommended_plan_id"] == "A"


def test_sim_chain_writes_no_rows(client):
    """模拟节点只有 {"label"}：与读侧空投影同一门槛，一行不落。"""
    run = create_run(client, create_project(client)["id"])
    approve_when_asked(client, run["id"], option_id="approve")
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    assert _rows(client, run["id"]) == []
