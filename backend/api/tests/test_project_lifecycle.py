"""项目维护（侧栏「最近任务」）：重命名、归档过滤与级联删除。"""

from __future__ import annotations

from conftest import API, create_project, create_run, register_user

from omm_api.models import new_id, utcnow
from omm_api.orm import ArtifactRow, ArtifactTextRow


def _project_names(client, archived: bool = False) -> list[str]:
    response = client.get(f"{API}/projects", params={"archived": archived})
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


def _add_artifact(app, project_id: str, run_id, content: bytes, name: str) -> str:
    sha, size = app.state.blobs.put(content)
    session = app.state.db.session_factory()
    try:
        artifact_id = new_id()
        session.add(
            ArtifactRow(
                id=artifact_id,
                project_id=project_id,
                run_id=run_id,
                kind="report",
                name=name,
                uri=f"local://{sha}",
                sha256=sha,
                size_bytes=size,
                media_type="text/plain",
                status="READY",
                created_at=utcnow(),
            )
        )
        session.add(
            ArtifactTextRow(
                artifact_id=artifact_id,
                status="ready",
                engine="plain",
                characters=size,
                text="缓存正文",
                created_at=utcnow(),
            )
        )
        session.commit()
        return sha
    finally:
        session.close()


def test_rename_project(client):
    project = create_project(client, "原始名称")

    updated = client.patch(f"{API}/projects/{project['id']}", json={"name": "改名后的任务"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "改名后的任务"

    fetched = client.get(f"{API}/projects/{project['id']}").json()
    assert fetched["name"] == "改名后的任务"
    assert "改名后的任务" in _project_names(client)


def test_patch_rejects_empty_name(client):
    project = create_project(client)
    response = client.patch(f"{API}/projects/{project['id']}", json={"name": ""})
    assert response.status_code == 422, response.text


def test_archive_hides_from_default_list_and_is_reversible(client):
    keep = create_project(client, "保留在列表")
    archived = create_project(client, "被归档任务")

    response = client.patch(f"{API}/projects/{archived['id']}", json={"archived": True})
    assert response.status_code == 200, response.text

    assert _project_names(client) == ["保留在列表"]
    assert _project_names(client, archived=True) == ["被归档任务"]
    # 归档不影响直接访问：工作台链接仍可打开
    assert client.get(f"{API}/projects/{archived['id']}").status_code == 200
    assert keep["id"] in {p["id"] for p in client.get(f"{API}/projects").json()["items"]}

    restored = client.patch(f"{API}/projects/{archived['id']}", json={"archived": False})
    assert restored.status_code == 200
    assert set(_project_names(client)) == {"保留在列表", "被归档任务"}
    assert _project_names(client, archived=True) == []


def test_delete_project_cascades_runs_artifacts_and_blobs(client):
    app = client.app
    doomed = create_project(client, "要删除的任务")
    survivor = create_project(client, "无关任务")
    run = create_run(client, doomed["id"], auto_start=False)

    unique_sha = _add_artifact(app, doomed["id"], run["id"], b"only-in-doomed", "独占.txt")
    shared_sha = _add_artifact(app, doomed["id"], None, b"shared-bytes", "共享.txt")
    assert _add_artifact(app, survivor["id"], None, b"shared-bytes", "共享副本.txt") == shared_sha

    deleted = client.delete(f"{API}/projects/{doomed['id']}")
    assert deleted.status_code == 204, deleted.text

    assert client.get(f"{API}/projects/{doomed['id']}").status_code == 404
    assert client.get(f"{API}/task-runs/{run['id']}").status_code == 404
    assert not app.state.blobs.exists(unique_sha), "无引用的内容对象应当回收"
    assert app.state.blobs.exists(shared_sha), "仍被其他项目引用的内容对象不得删除"
    assert client.get(f"{API}/projects/{survivor['id']}").status_code == 200


def test_project_maintenance_requires_ownership(client, second_client):
    project = create_project(client, "私有任务")

    # 未登录：v1 资源一律 401
    assert second_client.patch(
        f"{API}/projects/{project['id']}", json={"name": "别人改名"}
    ).status_code == 401
    assert second_client.delete(f"{API}/projects/{project['id']}").status_code == 401

    # 他人登录后：不泄露资源存在性，一律 404
    register_user(second_client, f"lifecycle-other-{new_id()[:8]}@test.dev")
    assert second_client.patch(
        f"{API}/projects/{project['id']}", json={"name": "别人改名"}
    ).status_code == 404
    assert second_client.delete(f"{API}/projects/{project['id']}").status_code == 404

    # 原主人的数据完好
    assert client.get(f"{API}/projects/{project['id']}").json()["name"] == "私有任务"
