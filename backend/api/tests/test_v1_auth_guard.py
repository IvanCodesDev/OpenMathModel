"""/api/v1 资源登录鉴权与归属隔离（C2）。

规则：
- 未登录访问 /api/v1 一律 401 AUTH_REQUIRED。
- 项目/任务归属创建者；他人访问一律 404（不泄露资源存在性）。
"""
from __future__ import annotations

from conftest import API, create_project, create_run, register_user


def test_anonymous_requests_are_rejected(second_client):
    cases = [
        ("GET", f"{API}/projects"),
        ("POST", f"{API}/projects"),
        ("GET", f"{API}/task-runs"),
        ("POST", f"{API}/task-runs"),
    ]
    for method, url in cases:
        response = second_client.request(method, url, json={} if method == "POST" else None)
        assert response.status_code == 401, f"{method} {url}: {response.text}"
        assert response.json()["code"] == "AUTH_REQUIRED"


def test_projects_are_isolated_between_users(client, second_client):
    mine = create_project(client, "用户A的项目")
    run = create_run(client, mine["id"], auto_start=False)

    register_user(second_client, "userb-isolation@test.dev")

    # 用户 B 列表里看不到 A 的资源
    assert second_client.get(f"{API}/projects").json()["total"] == 0
    assert second_client.get(f"{API}/task-runs").json()["total"] == 0

    # 直接访问 A 的资源一律 404
    assert second_client.get(f"{API}/projects/{mine['id']}").status_code == 404
    assert second_client.get(f"{API}/task-runs/{run['id']}").status_code == 404
    assert second_client.get(f"{API}/task-runs/{run['id']}/events/history").status_code == 404

    # 也不能在 A 的项目下建任务
    response = second_client.post(
        f"{API}/task-runs", json={"project_id": mine["id"], "goal": "越权尝试"}
    )
    assert response.status_code == 404

    # 也不能对 A 的任务下发动作
    response = second_client.post(
        f"{API}/task-runs/{run['id']}/actions", json={"action": "cancel"}
    )
    assert response.status_code == 404


def test_project_owner_is_current_user(client):
    project = create_project(client, "归属校验项目")
    me = client.get("/api/account/me").json()["user"]
    assert project["owner"] == me["id"]


def test_approval_actor_records_user_email(client):
    from conftest import approve_when_asked, run_status_is, wait_until

    project = create_project(client)
    run = create_run(client, project["id"], auto_start=False)
    approve_when_asked(client, run["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    approvals = client.get(f"{API}/task-runs/{run['id']}/approvals").json()["items"]
    resolved = [a for a in approvals if a["status"] == "RESOLVED"]
    assert resolved, approvals
    me = client.get("/api/account/me").json()["user"]
    assert resolved[0]["resolution"]["actor"] == me["email"]
