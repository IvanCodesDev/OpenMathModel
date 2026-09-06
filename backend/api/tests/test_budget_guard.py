"""预算治理接线（§4.7 C9）：run/node 级硬停在真实链路生效。

账本从 run.log 事件持久重建（每次 advance 重建治理器），因此这些用例同时
证明限额跨 tick 成立：每个阶段在独立的 advance 里执行，计数仍然累计。
预算默认值来自 harness（§4.7 唯一出处），环境变量仅作上限覆盖（失控保护
的临时追加通道；GB 审批闸门在后续批次提供）。
"""

from __future__ import annotations

from conftest import (
    approve_when_asked,
    create_project,
    create_run,
    run_status_is,
    wait_until,
)
from sqlalchemy import select

from omm_api.orm import LlmUsageRow
from test_task_runs_llm_nodes import (
    EXPERIMENT_OUTPUT,
    _configure_llm,
    _sandbox_reply,
    _saw_observation,
    _stage_router,
    _system_of,
    _wire_messages,
)


def test_run_level_llm_call_cap_hard_stops_with_e310(client, monkeypatch):
    """第 3 次模型调用越线（上限 2）：在花钱之前硬停，失败信息可行动。"""
    monkeypatch.setenv("OMM_RUN_MAX_LLM_CALLS", "2")
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")

    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))

    message = failed["failure"]["message"]
    assert "[E310]" in message, "运行级预算硬停必须带稳定错误码"
    assert "OMM_RUN_MAX_LLM_CALLS" in message, "失败信息必须给出可行动的追加通道"

    # 前两个阶段各花掉一次调用后成功；第三个阶段在调用前被拦下
    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    statuses = {step["node"]: step["status"] for step in steps}
    assert statuses["PROBLEM_ANALYSIS"] == "SUCCEEDED"
    assert statuses["DATA_PREPARATION"] == "SUCCEEDED"
    assert statuses["MODEL_PLANNING"] == "FAILED"


def test_node_level_token_cap_hard_stops_with_e320(client, monkeypatch):
    """节点 token 上限只打节点自己：stub 每次调用 30 tokens，上限 200——题意、数据
    两阶段各一次调用无碍；方案阶段三路提议（会话：检索信封 + 终答，每路 2 次）+
    归约 + 规范化共 8 次调用，第 8 次（规范化）调用前累计 210 越线被拦。若账本是
    跨节点累计的，提议人第 6 次调用前就已累计 210（题意 30 + 数据 30 + 提议 150），
    用量行只会有 7 行而不是 9 行。

    上限刻意放在并行 fan-out 之外的串行段（归约 / 规范化）：预检读的是已记账的
    账本、记账在调用返回之后，三路提议人并行时若边界落在 fan-out 里，两路可能都在
    同一刻度过检（越界窗口 ≤ 并行度 − 1 次，§4.7 已知取舍），行数就不确定。"""
    monkeypatch.setenv("OMM_NODE_MAX_TOKENS", "200")
    _configure_llm(client, monkeypatch)
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")

    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))

    message = failed["failure"]["message"]
    assert "[E320]" in message
    assert "MODEL_PLANNING" in message

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    statuses = {step["node"]: step["status"] for step in steps}
    for node in ("PROBLEM_ANALYSIS", "DATA_PREPARATION"):
        assert statuses[node] == "SUCCEEDED", f"{node} 不应被方案节点的预算殃及"
    assert statuses["MODEL_PLANNING"] == "FAILED"
    # 花钱之前硬停：前 7 次方案阶段调用都记了账，第 8 次没有用量行
    with client.app.state.db.session_factory() as session:
        usage_rows = session.execute(
            select(LlmUsageRow).where(LlmUsageRow.run_id == run["id"])
        ).scalars().all()
    assert len(usage_rows) == 9, "题意 1 + 数据 1 + 提议 3×2 + 归约 1；规范化在调用前被拦"


def test_sandbox_run_cap_charges_upfront_with_e310(client, monkeypatch):
    """沙箱运行按次预付：上限 1 次时，第一波代码失败后的修复重跑（第 2 次
    运行）在启动之前就被拦下（started run is spent money，§4.7）。"""
    monkeypatch.setenv("OMM_RUN_MAX_SANDBOX_RUNS", "1")
    envelopes_sent: list[str] = []

    def router(request):
        messages = _wire_messages(request)
        if "实验工程师" in _system_of(messages):
            # 第一波交会失败的代码；修复波（终答前无观察的新会话）交修复版，
            # 但第 2 次 python_run 必须在启动前被预算闸拦下。
            code = (
                "raise RuntimeError('bad seed')"
                if not envelopes_sent
                else "print('OMM_METRICS_JSON: {\"rmse\": 0.5}')"
            )
            if not _saw_observation(messages):
                envelopes_sent.append(code)
            return _sandbox_reply(messages, code, EXPERIMENT_OUTPUT)
        return _stage_router(request)

    _configure_llm(client, monkeypatch, handler=router)
    run = create_run(client, create_project(client)["id"], goal="优化共享单车调度")

    approve_when_asked(client, run["id"], option_id="approve")
    failed = wait_until(client, run["id"], run_status_is(client, run["id"], "FAILED"))

    message = failed["failure"]["message"]
    assert "[E310]" in message
    assert "沙箱" in message

    steps = client.get(f"/api/v1/task-runs/{run['id']}/steps").json()["items"]
    statuses = {step["node"]: step["status"] for step in steps}
    assert statuses["EXPERIMENTING"] == "FAILED"
