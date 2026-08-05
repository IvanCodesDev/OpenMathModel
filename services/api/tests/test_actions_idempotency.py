from __future__ import annotations

from conftest import API, create_project, create_run, run_status_is, wait_until


def test_invalid_action_returns_conflict_envelope(client):
    project = create_project(client)
    run = create_run(client, project["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "RUNNING"))

    response = client.post(f"{API}/task-runs/{run['id']}/actions", json={"action": "approve"})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "INVALID_ACTION"
    assert body["request_id"]


def test_unknown_project_returns_not_found(client):
    response = client.post(
        f"{API}/task-runs",
        json={"project_id": "proj_" + "f" * 32, "goal": "x"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_create_task_run_is_idempotent_with_key(client):
    project = create_project(client)
    body = {
        "project_id": project["id"],
        "goal": "幂等创建",
        "auto_start": False,
    }
    headers = {"Idempotency-Key": "create-key-1"}

    first = client.post(f"{API}/task-runs", json=body, headers=headers)
    second = client.post(f"{API}/task-runs", json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    runs = client.get(f"{API}/task-runs", params={"project_id": project["id"]}).json()["items"]
    assert len(runs) == 1, "同一幂等键重复请求不应创建第二个运行"


def test_same_key_different_body_is_rejected(client):
    project = create_project(client)
    headers = {"Idempotency-Key": "create-key-2"}
    base = {"project_id": project["id"], "goal": "内容 A", "auto_start": False}

    first = client.post(f"{API}/task-runs", json=base, headers=headers)
    assert first.status_code == 201

    conflicting = client.post(
        f"{API}/task-runs", json={**base, "goal": "内容 B"}, headers=headers
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_action_idempotency_replays_first_response(client):
    project = create_project(client)
    run = create_run(client, project["id"])
    run_id = run["id"]
    wait_until(client, run_id, run_status_is(client, run_id, "RUNNING"))

    headers = {"Idempotency-Key": "pause-key-1"}
    first = client.post(
        f"{API}/task-runs/{run_id}/actions", json={"action": "pause"}, headers=headers
    )
    assert first.status_code == 200
    assert first.json()["status"] == "PAUSED"

    # 同一幂等键重放首次响应（字节级一致）
    replay = client.post(
        f"{API}/task-runs/{run_id}/actions", json={"action": "pause"}, headers=headers
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # 等价重复动作（无键）幂等返回当前状态
    bare = client.post(f"{API}/task-runs/{run_id}/actions", json={"action": "pause"})
    assert bare.status_code == 200
    assert bare.json()["status"] == "PAUSED"
