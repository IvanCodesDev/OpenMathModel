"""运行中用户备注（§11.3 方案 A）：POST 落行、run.log 回执、提示词注入选择。

通道语义的三条边界：不打断当前执行（append-only + 下一次节点执行生效）、
scope 指向已完成阶段时回执附带回退引导（重做由人显式操作）、终态运行拒收。
注入选择逻辑（global/stage 过滤与格式）在 notes_prompt_block 纯函数层单测；
EngineLlmPort 的实际拼接位置由该函数的唯一调用点保证。
"""

from __future__ import annotations

from conftest import API, create_project, create_run, register_user

from omm_api.llm import notes_prompt_block


def _post_note(client, run_id: str, text: str = "模型请优先考虑季节性因素", **extra):
    return client.post(f"{API}/task-runs/{run_id}/notes", json={"text": text, **extra})


def _note_events(client, run_id: str) -> list[dict]:
    events = client.get(f"{API}/task-runs/{run_id}/events/history").json()["items"]
    return [e["payload"] for e in events if e["payload"].get("kind") == "user_note"]


def test_post_note_records_and_emits_receipt(client) -> None:
    project = create_project(client)
    run = create_run(client, project["id"])

    response = _post_note(client, run["id"])
    assert response.status_code == 201, response.text
    note = response.json()
    assert note["scope"] == "global" and note["run_id"] == run["id"]
    assert note["text"] == "模型请优先考虑季节性因素"

    receipts = _note_events(client, run["id"])
    assert len(receipts) == 1
    assert receipts[0]["note_id"] == note["id"]
    assert "后续每次节点执行" in receipts[0]["message"]


def test_stage_scope_on_completed_stage_gets_redo_guidance(client) -> None:
    project = create_project(client)
    run = create_run(client, project["id"])
    # sim 链推进到 PROBLEM_ANALYSIS 完成（tick 驱动，确定性）
    for _ in range(4):
        client.app.state.advancer.advance(run["id"])
    steps = client.get(f"{API}/task-runs/{run['id']}/steps").json()["items"]
    assert any(
        s["node"] == "PROBLEM_ANALYSIS" and s["status"] == "SUCCEEDED" for s in steps
    ), steps

    response = _post_note(client, run["id"], scope="PROBLEM_ANALYSIS")
    assert response.status_code == 201, response.text
    message = _note_events(client, run["id"])[0]["message"]
    assert "已完成" in message and "重试或回退" in message


def test_stage_scope_on_pending_stage_no_guidance(client) -> None:
    project = create_project(client)
    run = create_run(client, project["id"])
    response = _post_note(client, run["id"], scope="PAPER_WRITING")
    assert response.status_code == 201
    message = _note_events(client, run["id"])[0]["message"]
    assert "已完成" not in message and "论文" in message


def test_terminal_run_rejects_notes(client) -> None:
    project = create_project(client)
    run = create_run(client, project["id"])
    cancel = client.post(
        f"{API}/task-runs/{run['id']}/actions", json={"action": "cancel"}
    )
    assert cancel.status_code == 200
    response = _post_note(client, run["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "RUN_FINISHED"


def test_validation_rejects_bad_payloads(client) -> None:
    project = create_project(client)
    run = create_run(client, project["id"])
    assert _post_note(client, run["id"], scope="BOGUS").status_code == 422
    assert _post_note(client, run["id"], text="").status_code == 422
    assert _post_note(client, run["id"], text="   ").status_code == 422


def test_other_user_cannot_note_foreign_run(client, second_client) -> None:
    project = create_project(client)
    run = create_run(client, project["id"])
    register_user(second_client, "intruder-notes@test.dev")
    response = second_client.post(
        f"{API}/task-runs/{run['id']}/notes", json={"text": "别人的运行"}
    )
    assert response.status_code == 404


# -- 注入选择（纯函数层） --------------------------------------------------------


def test_notes_block_global_applies_everywhere() -> None:
    block = notes_prompt_block([("global", "统一用中文变量名")], node_id="EXPERIMENTING")
    assert "用户补充要求" in block and "统一用中文变量名" in block


def test_notes_block_stage_scope_filters_by_node() -> None:
    notes = [("PAPER_WRITING", "摘要控制在 300 字"), ("global", "引用要带出处")]
    in_paper = notes_prompt_block(notes, node_id="PAPER_WRITING")
    assert "摘要控制在 300 字" in in_paper and "引用要带出处" in in_paper
    in_experiment = notes_prompt_block(notes, node_id="EXPERIMENTING")
    assert "摘要控制在 300 字" not in in_experiment and "引用要带出处" in in_experiment


def test_notes_block_empty_when_no_match() -> None:
    assert notes_prompt_block([("PAPER_WRITING", "x")], node_id="EXPERIMENTING") == ""
    assert notes_prompt_block([], node_id=None) == ""
