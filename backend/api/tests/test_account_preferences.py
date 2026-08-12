"""用户偏好（高级设置）：最大并发任务的读写、边界与创建任务闸门。"""

from __future__ import annotations

from conftest import API, create_project, create_run, register_user


def _preferences(client) -> dict:
    response = client.get("/api/account/preferences")
    assert response.status_code == 200, response.text
    return response.json()["preferences"]


def _try_create_run(client, project_id: str):
    return client.post(f"{API}/task-runs", json={"project_id": project_id, "goal": "完成基线建模"})


def test_preferences_default_and_roundtrip(client):
    assert _preferences(client)["max_concurrent_runs"] == 3

    updated = client.put("/api/account/preferences", json={"max_concurrent_runs": 5})
    assert updated.status_code == 200, updated.text
    assert updated.json()["preferences"]["max_concurrent_runs"] == 5
    assert _preferences(client)["max_concurrent_runs"] == 5


def test_preferences_reject_out_of_range_values(client):
    for bad in (0, 9, -1, "abc"):
        response = client.put("/api/account/preferences", json={"max_concurrent_runs": bad})
        assert response.status_code == 422, f"{bad} 应被拒绝: {response.text}"
    # 非法值不落库
    assert _preferences(client)["max_concurrent_runs"] == 3


def test_preferences_require_login(second_client):
    assert second_client.get("/api/account/preferences").status_code == 401
    assert second_client.put(
        "/api/account/preferences", json={"max_concurrent_runs": 2}
    ).status_code == 401


def test_create_run_blocked_at_default_limit(client):
    project = create_project(client)
    for _ in range(3):
        create_run(client, project["id"])

    blocked = _try_create_run(client, project["id"])
    assert blocked.status_code == 409, blocked.text
    payload = blocked.json()
    assert payload["code"] == "CONCURRENCY_LIMIT"
    assert "3" in payload["message"]


def test_cancelling_a_run_frees_a_concurrency_slot(client):
    client.put("/api/account/preferences", json={"max_concurrent_runs": 1})
    project = create_project(client)
    first = create_run(client, project["id"])

    assert _try_create_run(client, project["id"]).status_code == 409

    cancelled = client.post(
        f"{API}/task-runs/{first['id']}/actions", json={"action": "cancel"}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert _try_create_run(client, project["id"]).status_code == 201


def test_limit_counts_runs_across_projects_of_the_same_user(client):
    client.put("/api/account/preferences", json={"max_concurrent_runs": 1})
    create_run(client, create_project(client)["id"])

    other_project = create_project(client, "第二个项目")
    blocked = _try_create_run(client, other_project["id"])
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "CONCURRENCY_LIMIT"


def test_limit_is_per_user_not_global(client, second_client):
    client.put("/api/account/preferences", json={"max_concurrent_runs": 1})
    create_run(client, create_project(client)["id"])

    register_user(second_client, "concurrency-other@test.dev")
    other_project = create_project(second_client)
    assert _try_create_run(second_client, other_project["id"]).status_code == 201


def test_waiting_approval_does_not_occupy_a_slot(client, tick):
    """等待审批的任务停在人身上，不占并发额度。"""
    client.put("/api/account/preferences", json={"max_concurrent_runs": 1})
    project = create_project(client)
    run = create_run(client, project["id"])

    # 推进到 WAITING_APPROVAL（模拟工作流在方案确认处停下）
    from conftest import pending_approval, wait_until

    wait_until(client, run["id"], pending_approval(client, run["id"]))
    assert _try_create_run(client, project["id"]).status_code == 201, "等待审批不应占用并发位"
