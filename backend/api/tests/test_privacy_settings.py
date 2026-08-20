"""数据与隐私：设置读写、任务保留清扫、文件缓存清理与产物内容回收。"""

from __future__ import annotations

from datetime import timedelta

from conftest import API, create_project, create_run

from omm_api.models import new_id, utcnow
from omm_api.orm import ArtifactRow, ArtifactTextRow, TaskRunRow
from omm_api.privacy import DEFAULT_PRIVACY_SETTINGS, run_retention_sweep


def _settings(client) -> dict:
    response = client.get("/api/account/privacy-settings")
    assert response.status_code == 200, response.text
    return response.json()["settings"]


def _put_settings(client, **overrides):
    response = client.put("/api/account/privacy-settings", json=overrides)
    assert response.status_code == 200, response.text
    return response.json()["settings"]


def _cancelled_run(client, project_id: str) -> str:
    run = create_run(client, project_id, auto_start=False)
    cancelled = client.post(f"{API}/task-runs/{run['id']}/actions", json={"action": "cancel"})
    assert cancelled.status_code == 200, cancelled.text
    return run["id"]


def _backdate_run(app, run_id: str, days: float) -> None:
    session = app.state.db.session_factory()
    try:
        row = session.get(TaskRunRow, run_id)
        row.ended_at = utcnow() - timedelta(days=days)
        session.commit()
    finally:
        session.close()


def _sweep(app) -> dict:
    session = app.state.db.session_factory()
    try:
        counts = run_retention_sweep(session, app.state.blobs)
        session.commit()
        return counts
    finally:
        session.close()


def test_privacy_settings_default_and_roundtrip(client):
    assert _settings(client) == DEFAULT_PRIVACY_SETTINGS

    saved = _put_settings(client, retention="days_30", notify_budget=False)
    assert saved["retention"] == "days_30"
    assert saved["notify_budget"] is False
    # 未提交的键落为默认值，读取时保持完整九项
    assert _settings(client)["file_cache"] == "days_30"


def test_privacy_settings_reject_unknown_policy(client):
    # 布尔项沿用 pydantic 宽松转换（与用量设置一致）；策略枚举必须严格拒绝未知值
    for field, bad in (("retention", "days_5"), ("file_cache", "never"), ("retention", 30)):
        response = client.put("/api/account/privacy-settings", json={field: bad})
        assert response.status_code == 422, f"{field}={bad} 应被拒绝: {response.text}"
    assert _settings(client) == DEFAULT_PRIVACY_SETTINGS


def test_privacy_settings_require_login(second_client):
    assert second_client.get("/api/account/privacy-settings").status_code == 401
    assert second_client.put(
        "/api/account/privacy-settings", json={"retention": "days_30"}
    ).status_code == 401


def test_retention_sweep_removes_expired_terminal_runs(client):
    project = create_project(client)
    expired = _cancelled_run(client, project["id"])
    fresh = _cancelled_run(client, project["id"])
    active = create_run(client, project["id"], auto_start=False)["id"]

    _put_settings(client, retention="days_30")
    _backdate_run(client.app, expired, days=40)
    _backdate_run(client.app, fresh, days=5)

    counts = _sweep(client.app)
    assert counts["runs"] == 1

    assert client.get(f"{API}/task-runs/{expired}").status_code == 404
    assert client.get(f"{API}/task-runs/{fresh}").status_code == 200
    assert client.get(f"{API}/task-runs/{active}").status_code == 200


def test_retention_defaults_keep_everything(client):
    """从未保存过设置、或保留策略为永久的用户，清扫不触碰其数据。"""
    project = create_project(client)
    old_run = _cancelled_run(client, project["id"])
    _backdate_run(client.app, old_run, days=365)

    assert _sweep(client.app) == {"runs": 0, "texts": 0}

    _put_settings(client, retention="forever")
    assert _sweep(client.app) == {"runs": 0, "texts": 0}
    assert client.get(f"{API}/task-runs/{old_run}").status_code == 200


def test_retention_on_complete_waits_for_grace_hour(client):
    project = create_project(client)
    just_done = _cancelled_run(client, project["id"])
    seen_enough = _cancelled_run(client, project["id"])

    _put_settings(client, retention="on_complete")
    _backdate_run(client.app, seen_enough, days=0.5)

    assert _sweep(client.app)["runs"] == 1
    assert client.get(f"{API}/task-runs/{just_done}").status_code == 200
    assert client.get(f"{API}/task-runs/{seen_enough}").status_code == 404


def test_retention_removes_artifacts_and_unreferenced_blobs(client):
    """随任务删除产物行与正文缓存；内容对象仅在库内无其他引用时回收。"""
    app = client.app
    project = create_project(client)
    doomed = _cancelled_run(client, project["id"])
    survivor = _cancelled_run(client, project["id"])

    shared_sha, shared_size = app.state.blobs.put(b"shared-bytes")
    unique_sha, unique_size = app.state.blobs.put(b"only-in-doomed-run")

    session = app.state.db.session_factory()
    try:
        for run_id, sha, size, name in (
            (doomed, shared_sha, shared_size, "共享.txt"),
            (doomed, unique_sha, unique_size, "独占.txt"),
            (survivor, shared_sha, shared_size, "共享副本.txt"),
        ):
            artifact_id = new_id()
            session.add(
                ArtifactRow(
                    id=artifact_id,
                    project_id=project["id"],
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
    finally:
        session.close()

    _put_settings(client, retention="days_30", file_cache="days_30")
    _backdate_run(app, doomed, days=45)

    counts = _sweep(app)
    assert counts["runs"] == 1
    assert counts["texts"] == 0  # doomed 的缓存随产物删除，不计入文件缓存清理

    session = app.state.db.session_factory()
    try:
        remaining = session.query(ArtifactRow).filter(ArtifactRow.run_id == doomed).count()
        assert remaining == 0
        survivor_artifacts = (
            session.query(ArtifactRow).filter(ArtifactRow.run_id == survivor).count()
        )
        assert survivor_artifacts == 1
    finally:
        session.close()

    assert app.state.blobs.exists(shared_sha), "仍被引用的内容对象不得删除"
    assert not app.state.blobs.exists(unique_sha), "无引用的内容对象应当回收"


def test_file_cache_sweep_by_age_and_on_close(client):
    app = client.app
    project = create_project(client)
    closed = _cancelled_run(client, project["id"])
    active = create_run(client, project["id"], auto_start=False)["id"]

    session = app.state.db.session_factory()
    try:
        rows = {}
        for run_id, age_days in ((closed, 10), (active, 10), (active, 0)):
            artifact_id = new_id()
            session.add(
                ArtifactRow(
                    id=artifact_id,
                    project_id=project["id"],
                    run_id=run_id,
                    kind="input",
                    name="附件.txt",
                    status="READY",
                    created_at=utcnow(),
                )
            )
            session.add(
                ArtifactTextRow(
                    artifact_id=artifact_id,
                    status="ready",
                    engine="plain",
                    characters=4,
                    text="正文",
                    created_at=utcnow() - timedelta(days=age_days),
                )
            )
            rows[(run_id, age_days)] = artifact_id
        session.commit()
    finally:
        session.close()

    # 7 天策略：两条 10 天前的缓存被清（无论任务状态），今天的保留
    _put_settings(client, file_cache="days_7")
    assert _sweep(app)["texts"] == 2

    # on_close 策略：已结束任务的剩余缓存被清，进行中任务的保留
    session = app.state.db.session_factory()
    try:
        session.add(
            ArtifactTextRow(
                artifact_id=rows[(closed, 10)],
                status="ready",
                engine="plain",
                characters=4,
                text="重建的缓存",
                created_at=utcnow(),
            )
        )
        session.commit()
    finally:
        session.close()

    _put_settings(client, file_cache="on_close")
    assert _sweep(app)["texts"] == 1

    session = app.state.db.session_factory()
    try:
        left = session.query(ArtifactTextRow).count()
        assert left == 1  # 只剩 active 任务今天的缓存
    finally:
        session.close()


def test_sweep_never_touches_other_users(client, second_client):
    """清扫按用户各自的策略执行，一个用户的激进策略不影响他人数据。"""
    from conftest import register_user

    register_user(second_client, f"privacy-other-{new_id()[:8]}@test.dev")
    other_project = create_project(second_client, "他人项目")
    other_run = _cancelled_run(second_client, other_project["id"])
    _backdate_run(second_client.app, other_run, days=400)

    _put_settings(client, retention="days_30")
    assert _sweep(client.app)["runs"] == 0
    assert second_client.get(f"{API}/task-runs/{other_run}").status_code == 200
