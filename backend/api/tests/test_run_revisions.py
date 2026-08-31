"""修订回合（ADR-0013）：已完成的运行按用户要求重开、重做、或撤回。

覆盖这条通道上最容易做错的四件事：重开的运行不能还挂着「已结束」的痕迹；
重做起点必须由人在审批门里定（服务端只给建议）；撤回要回到 COMPLETED 而不是
判成失败；重做只能重跑选定阶段及其下游，上游成果原样留着。
"""

from __future__ import annotations

from conftest import (
    API,
    approve_when_asked,
    create_project,
    create_run,
    get_run,
    pending_approval,
    register_user,
    run_status_is,
    wait_until,
)

from omm_api.engine_glue import (
    MAX_REVISION_ROUNDS,
    _run_budget_from_env,
    suggest_revision_stage,
)


def _completed_run(client) -> str:
    project = create_project(client)
    run = create_run(client, project["id"])
    approve_when_asked(client, run["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))
    return run["id"]


def _post_revision(client, run_id: str, text: str = "结论部分论证不足，请重写"):
    return client.post(f"{API}/task-runs/{run_id}/revisions", json={"text": text})


def _steps(client, run_id: str) -> list[dict]:
    return client.get(f"{API}/task-runs/{run_id}/steps").json()["items"]


def _attempts(client, run_id: str, node: str) -> list[int]:
    return [s["attempt"] for s in _steps(client, run_id) if s["node"] == node]


def test_revision_reopens_run_and_asks_where_to_restart(client) -> None:
    run_id = _completed_run(client)

    response = _post_revision(client, run_id)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["round"] == 1
    assert receipt["suggested_stage"] == "PAPER_WRITING"
    assert receipt["approval_id"]

    run = get_run(client, run_id)
    assert run["status"] == "WAITING_APPROVAL"
    # 重开的运行不能还带着终态时间戳，否则列表与详情仍按已完成排布
    assert run["ended_at"] is None

    approval = pending_approval(client, run_id)()
    assert approval is not None and approval["id"] == receipt["approval_id"]
    option_ids = [option["id"] for option in approval["options"]]
    assert option_ids == [
        "redo:PROBLEM_ANALYSIS",
        "redo:DATA_PREPARATION",
        "redo:MODEL_PLANNING",
        "redo:EXPERIMENTING",
        "redo:VALIDATING",
        "redo:PAPER_WRITING",
        "reject",
    ]
    # 恰一个 recommended 是硬要求：workspace_view._preferred_option 只在推荐项
    # 唯一时才预选 CTA，标多了或标漏了按钮就是 no-op。其余项落库时不写该键，
    # 上线序列化补成 null（契约里 null 等价于 false）。
    assert [o["id"] for o in approval["options"] if o.get("recommended")] == [
        "redo:PAPER_WRITING"
    ], "建议项只标记、不代选"
    assert all(
        o["recommended"] in (None, False)
        for o in approval["options"]
        if o["id"] != "redo:PAPER_WRITING"
    )


def test_revision_text_is_stored_as_a_global_note(client) -> None:
    """要求正文要进 run_notes：重做的节点靠它读到「要改什么」。"""
    run_id = _completed_run(client)
    response = _post_revision(client, run_id, "摘要请压到 300 字以内")
    note_id = response.json()["note_id"]

    events = client.get(f"{API}/task-runs/{run_id}/events/history").json()["items"]
    receipts = [
        e["payload"] for e in events if e["payload"].get("kind") == "revision_requested"
    ]
    assert len(receipts) == 1
    assert receipts[0]["note_id"] == note_id
    assert receipts[0]["round"] == 1
    assert "第 1 轮" in receipts[0]["message"]


def test_approving_revision_reruns_chosen_stage_and_downstream(client) -> None:
    run_id = _completed_run(client)
    _post_revision(client, run_id)

    approve_when_asked(client, run_id, option_id="redo:VALIDATING")
    run = get_run(client, run_id)
    assert run["status"] == "RUNNING"
    assert run["current_node"] == "VALIDATING", "重开后不能还停在 COMPLETED"

    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))
    assert _attempts(client, run_id, "VALIDATING") == [1, 2]
    assert _attempts(client, run_id, "PAPER_WRITING") == [1, 2]
    # 上游本轮不重做：它们的成果正是这一轮返工的依据
    assert _attempts(client, run_id, "EXPERIMENTING") == [1]
    assert _attempts(client, run_id, "PROBLEM_ANALYSIS") == [1]

    final = get_run(client, run_id)
    assert final["ended_at"] is not None and final["failure"] is None


def test_redo_from_planning_reraises_the_plan_gate(client) -> None:
    """从建模方案重做要重新过方案确认门——那道门本来就是该阶段完成时提的。"""
    run_id = _completed_run(client)
    _post_revision(client, run_id, "模型选得不合适，换个方法")

    approve_when_asked(client, run_id, option_id="redo:MODEL_PLANNING")
    approve_when_asked(client, run_id, option_id="approve")
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))

    assert _attempts(client, run_id, "MODEL_PLANNING") == [1, 2]
    approvals = client.get(f"{API}/task-runs/{run_id}/approvals").json()["items"]
    assert len(approvals) == 3, "首轮方案门 + 修订门 + 重做后的方案门"
    assert all(a["status"] == "RESOLVED" for a in approvals)


def test_declining_revision_restores_completed(client) -> None:
    run_id = _completed_run(client)
    before = get_run(client, run_id)
    _post_revision(client, run_id)

    approve_when_asked(client, run_id, option_id="reject")
    run = get_run(client, run_id)
    assert run["status"] == "COMPLETED", "什么都没跑坏，撤回不该判成失败"
    assert run["failure"] is None
    assert run["ended_at"] is not None
    assert run["current_node"] == before["current_node"] == "COMPLETED"

    # 撤回后一步也不许再跑
    counts = {s["node"]: s["attempt"] for s in _steps(client, run_id)}
    client.app.state.advancer.advance(run_id)
    assert {s["node"]: s["attempt"] for s in _steps(client, run_id)} == counts


def test_revision_rounds_are_capped(client) -> None:
    run_id = _completed_run(client)
    for expected_round in range(1, MAX_REVISION_ROUNDS + 1):
        response = _post_revision(client, run_id)
        assert response.status_code == 201, response.text
        assert response.json()["round"] == expected_round
        approve_when_asked(client, run_id, option_id="reject")

    response = _post_revision(client, run_id)
    assert response.status_code == 409
    assert response.json()["code"] == "REVISION_LIMIT_REACHED"


def test_unfinished_run_cannot_be_revised(client) -> None:
    project = create_project(client)
    run = create_run(client, project["id"])
    response = _post_revision(client, run["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "RUN_NOT_COMPLETED"


def test_revision_input_is_validated(client) -> None:
    run_id = _completed_run(client)
    assert _post_revision(client, run_id, text="").status_code == 422
    assert _post_revision(client, run_id, text="   ").status_code == 422


def test_other_user_cannot_revise_foreign_run(client, second_client) -> None:
    run_id = _completed_run(client)
    register_user(second_client, "intruder-revisions@test.dev")
    response = second_client.post(
        f"{API}/task-runs/{run_id}/revisions", json={"text": "别人的运行"}
    )
    assert response.status_code == 404


def test_revision_approval_matches_contract(client, validate_contract) -> None:
    run_id = _completed_run(client)
    _post_revision(client, run_id)
    approvals = client.get(f"{API}/task-runs/{run_id}/approvals").json()["items"]
    for approval in approvals:
        validate_contract("approval-request.schema.json", approval)


# -- 起点建议（纯函数层） --------------------------------------------------------


def test_suggestion_defaults_to_the_cheapest_reading() -> None:
    assert suggest_revision_stage("再润色一下") == "PAPER_WRITING"


def test_suggestion_picks_the_earliest_stage_mentioned() -> None:
    assert suggest_revision_stage("数据口径不对，实验也要重跑") == "DATA_PREPARATION"
    assert suggest_revision_stage("误差分析不够，论文也要改") == "VALIDATING"
    assert suggest_revision_stage("题意理解错了") == "PROBLEM_ANALYSIS"


def test_each_round_appends_one_more_quota() -> None:
    """账本是全 run 累计的，不按轮追加配额第二轮必然撞上首轮的额度（§3.1）。"""
    first = _run_budget_from_env(1)
    third = _run_budget_from_env(3)
    assert third.max_total_tokens == first.max_total_tokens * 3
    assert third.max_llm_calls == first.max_llm_calls * 3
    assert third.max_sandbox_runs == first.max_sandbox_runs * 3
