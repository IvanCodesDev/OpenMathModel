"""调度档位开关（§4.9）：OMM_GRAPH=off|shadow|linear-v1。

Graph v1（linear-v1）是现有六阶段线性推进的图化形式（§6.1 三步走第一步），影子
等价（§6.5）只比控制流——事件类型序列、状态转移、步骤/attempt 计数、审批点位置——
不比 outputs 正文、时间戳、id。第二步「等价证明后切换默认」已切：缺省 linear-v1
图驱动、线性当影子。这里锚定 API 装配点的三条边界：图驱动整链与线性推进控制流
逐一相等；缺省档一趟下来零分歧警告；非法值按缺省处理并留警告（不得静默换档）。
"""

from __future__ import annotations

import logging

from conftest import API, approve_when_asked, create_project, create_run, run_status_is, wait_until
from omm_agent_core import DEFAULT_GRAPH_MODE

from omm_api.engine_glue import _graph_mode

DIVERGENCE_MARK = "graph shadow divergence"


def _control_flow(client, run_id: str) -> list[tuple]:
    """事件史的控制流投影（§6.5 口径）：类型 + 控制字段，内容字段一律不看。"""
    items = client.get(f"{API}/task-runs/{run_id}/events/history").json()["items"]
    return [
        (
            event["type"],
            event["payload"].get("from"),
            event["payload"].get("to"),
            event["payload"].get("state"),
            event["payload"].get("attempt"),
            event["payload"].get("resume_state"),
            event["payload"].get("approved"),
            event["payload"].get("rerun"),
            event["payload"].get("target_state"),
        )
        for event in items
    ]


def _drive_reject_then_approve(client) -> str:
    """G1 退回重做 → 方案阶段 attempt 2 → 再弹 G1 → 批准 → 跑完：API 侧最曲折的控制流。"""
    project = create_project(client)
    run = create_run(client, project["id"])
    approve_when_asked(client, run["id"], option_id="reject")
    approve_when_asked(client, run["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    return run["id"]


def _steps(client, run_id: str) -> list[tuple[str, int, str]]:
    items = client.get(f"{API}/task-runs/{run_id}/steps").json()["items"]
    return [(step["node"], step["attempt"], step["status"]) for step in items]


def test_graph_driven_run_matches_linear_control_flow(client, monkeypatch, caplog) -> None:
    monkeypatch.setenv("OMM_GRAPH", "off")
    baseline = _drive_reject_then_approve(client)

    monkeypatch.setenv("OMM_GRAPH", "linear-v1")
    assert _graph_mode() == "linear-v1"
    with caplog.at_level(logging.WARNING, logger="omm.engine"):
        graph_driven = _drive_reject_then_approve(client)

    assert _control_flow(client, graph_driven) == _control_flow(client, baseline)
    assert _steps(client, graph_driven) == _steps(client, baseline)
    assert [attempt for node, attempt, _ in _steps(client, graph_driven) if node == "MODEL_PLANNING"] == [1, 2]
    # 图驱动时线性当影子：每一步决策也相同，没有一条分歧进日志
    assert not [r for r in caplog.records if DIVERGENCE_MARK in r.getMessage()]


def test_default_mode_is_graph_driven_and_records_no_divergence(client, monkeypatch, caplog) -> None:
    monkeypatch.delenv("OMM_GRAPH", raising=False)
    assert _graph_mode() == DEFAULT_GRAPH_MODE == "linear-v1"

    with caplog.at_level(logging.WARNING, logger="omm.engine"):
        run_id = _drive_reject_then_approve(client)

    assert _steps(client, run_id)[-1] == ("PAPER_WRITING", 1, "SUCCEEDED")
    assert not [r for r in caplog.records if DIVERGENCE_MARK in r.getMessage()]


def test_invalid_mode_value_warns_and_behaves_as_default(client, monkeypatch, caplog) -> None:
    monkeypatch.setenv("OMM_GRAPH", "modeling-v2")  # §4.9 留位、尚未落地的档位
    with caplog.at_level(logging.WARNING, logger="omm.engine"):
        assert _graph_mode() == DEFAULT_GRAPH_MODE
    assert any("OMM_GRAPH='modeling-v2'" in r.getMessage() for r in caplog.records)

    # 非法值不影响推进：整链照常跑完
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="omm.engine"):
        run_id = _drive_reject_then_approve(client)
    assert _steps(client, run_id)[-1] == ("PAPER_WRITING", 1, "SUCCEEDED")
    assert not [r for r in caplog.records if DIVERGENCE_MARK in r.getMessage()]
