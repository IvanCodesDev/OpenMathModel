from __future__ import annotations

from omm_api.orm import ApprovalRequestRow, ArtifactRow
from omm_api.serialize import utcnow
from omm_api.workspace_view import _preferred_option

from conftest import (
    API,
    approve_when_asked,
    create_project,
    create_run,
    pending_approval,
    register_user,
    run_status_is,
    wait_until,
)


def _workspace(client, run_id: str) -> dict:
    response = client.get(f"{API}/task-runs/{run_id}/workspace")
    assert response.status_code == 200, response.text
    return response.json()


def _insert_artifact(
    app,
    *,
    artifact_id: str,
    project_id: str,
    run_id: str,
    status: str = "READY",
    uri: str | None = "local://" + "f" * 64 + "/artifact.bin",
    sha256: str | None = "f" * 64,
    size_bytes: int | None = 1,
    name: str = "artifact.bin",
) -> None:
    with app.state.db.session_factory() as session:
        session.add(
            ArtifactRow(
                id=artifact_id,
                project_id=project_id,
                run_id=run_id,
                kind="dataset",
                name=name,
                uri=uri,
                sha256=sha256,
                size_bytes=size_bytes,
                media_type="application/octet-stream",
                producer_step=None,
                status=status,
                created_at=utcnow(),
            )
        )
        session.commit()


def test_workspace_view_only_preselects_an_unambiguous_approval_option() -> None:
    approval = ApprovalRequestRow(
        id="appr_" + "c" * 32,
        run_id="run_" + "d" * 32,
        decision_type="confirm_plan",
        title="选择建模方案",
        options=[
            {"id": "plan-a", "label": "方案 A"},
            {"id": "plan-b", "label": "方案 B"},
            {"id": "reject", "label": "退回"},
        ],
        status="PENDING",
        requested_at=utcnow(),
    )

    assert _preferred_option(approval) is None
    approval.options = [
        {"id": "approve", "label": "确认"},
        {"id": "reject", "label": "退回"},
    ]
    assert _preferred_option(approval) == "approve"


def test_workspace_view_maps_queued_run_to_pending_page(
    client, validate_contract
) -> None:
    project = create_project(client, "排队状态投影")
    run = create_run(client, project["id"], auto_start=False)

    payload = _workspace(client, run["id"])
    validate_contract("modeling-workspace-view.schema.json", payload)

    assert payload["run_status"] == "QUEUED"
    assert payload["active_node"] == "CREATED"
    assert payload["active_page"] == "running"
    assert payload["agent"]["state"] == "QUEUED"
    assert {page["status"] for page in payload["pages"]} == {"PENDING"}


def test_workspace_view_maps_waiting_approval_to_model_page(
    client, validate_contract
) -> None:
    project = create_project(client, "前后端投影测试")
    run = create_run(client, project["id"], auto_start=True)
    approval = wait_until(client, run["id"], pending_approval(client, run["id"]))

    response = client.get(f"{API}/task-runs/{run['id']}/workspace")
    assert response.status_code == 200, response.text
    payload = response.json()
    validate_contract("modeling-workspace-view.schema.json", payload)

    assert payload["project_name"] == "前后端投影测试"
    assert payload["active_node"] == "MODEL_PLANNING"
    assert payload["active_page"] == "model"
    assert payload["suggested_route"] == "/workspace/model-plan"
    assert payload["agent"]["state"] == "WAITING_APPROVAL"
    assert payload["agent"]["action"]["kind"] == "approve"
    assert payload["agent"]["action"]["approval_id"] == approval["id"]
    assert [page["key"] for page in payload["pages"]] == [
        "running",
        "data",
        "model",
        "experiments",
        "editor",
        "complete",
    ]
    assert next(page for page in payload["pages"] if page["key"] == "model")["status"] == "WAITING_APPROVAL"


def test_workspace_view_exposes_artifacts_and_completed_page(client, validate_contract) -> None:
    project = create_project(client, "完整交付投影")
    run = create_run(client, project["id"], auto_start=True)
    approve_when_asked(client, run["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    response = client.get(f"{API}/task-runs/{run['id']}/workspace")
    assert response.status_code == 200, response.text
    payload = response.json()
    validate_contract("modeling-workspace-view.schema.json", payload)

    assert payload["active_page"] == "complete"
    assert payload["agent"]["state"] == "COMPLETED"
    assert next(page for page in payload["pages"] if page["key"] == "complete")["status"] == "SUCCEEDED"
    assert {artifact["producer_node"] for artifact in payload["artifacts"]} == {
        "EXPERIMENTING",
        "PAPER_WRITING",
    }
    assert all(artifact["name"] for artifact in payload["artifacts"])
    assert all(artifact["download_url"].startswith("/api/v1/artifacts/") for artifact in payload["artifacts"])
    complete_page = next(page for page in payload["pages"] if page["key"] == "complete")
    assert set(complete_page["artifact_ids"]) == {
        artifact["id"] for artifact in payload["artifacts"]
    }


def test_workspace_view_keeps_non_ready_artifact_non_downloadable(
    client, app, validate_contract
) -> None:
    project = create_project(client, "待生成产物投影")
    run = create_run(client, project["id"], auto_start=False)
    artifact_id = "art_" + "a" * 32
    _insert_artifact(
        app,
        artifact_id=artifact_id,
        project_id=project["id"],
        run_id=run["id"],
        status="PENDING",
        uri=None,
        sha256=None,
        size_bytes=None,
        name="pending-result.csv",
    )

    payload = _workspace(client, run["id"])
    validate_contract("modeling-workspace-view.schema.json", payload)
    artifact = next(item for item in payload["artifacts"] if item["id"] == artifact_id)

    assert artifact["status"] == "PENDING"
    assert artifact["size_bytes"] is None
    assert artifact["download_url"] is None


def test_workspace_view_only_links_ready_readable_local_artifacts(
    client, app, validate_contract
) -> None:
    project = create_project(client, "产物下载边界")
    run = create_run(client, project["id"], auto_start=False)
    digest, size = app.state.blobs.put(b"downloadable")

    cases = [
        ("a", "READY", f"local://{digest}/valid.bin", digest, True),
        ("b", "PENDING", f"local://{digest}/pending.bin", digest, False),
        ("c", "READY", f"s3://bucket/{digest}", digest, False),
        ("d", "READY", "local://" + "e" * 64 + "/missing.bin", "e" * 64, False),
        ("e", "READY", f"local://{digest}/mismatch.bin", "d" * 64, False),
    ]
    for suffix, status, uri, sha256, _ in cases:
        _insert_artifact(
            app,
            artifact_id="art_" + suffix * 32,
            project_id=project["id"],
            run_id=run["id"],
            status=status,
            uri=uri,
            sha256=sha256,
            size_bytes=size,
            name=f"{suffix}.bin",
        )

    payload = _workspace(client, run["id"])
    validate_contract("modeling-workspace-view.schema.json", payload)
    artifacts = {item["id"]: item for item in payload["artifacts"]}

    for suffix, _, _, _, downloadable in cases:
        download_url = artifacts["art_" + suffix * 32]["download_url"]
        if downloadable:
            assert download_url == f"/api/v1/artifacts/art_{suffix * 32}/download"
        else:
            assert download_url is None


def test_workspace_view_excludes_artifact_with_mismatched_project(
    client, second_client, app, validate_contract
) -> None:
    project = create_project(client, "运行所属项目")
    run = create_run(client, project["id"], auto_start=False)
    register_user(second_client, "workspace-artifact-other@test.dev")
    foreign_project = create_project(second_client, "其他账户项目")
    foreign_artifact_id = "art_" + "b" * 32
    _insert_artifact(
        app,
        artifact_id=foreign_artifact_id,
        project_id=foreign_project["id"],
        run_id=run["id"],
        name="foreign-secret.bin",
    )

    payload = _workspace(client, run["id"])
    validate_contract("modeling-workspace-view.schema.json", payload)

    assert foreign_artifact_id not in {item["id"] for item in payload["artifacts"]}
    assert all(
        foreign_artifact_id not in page["artifact_ids"] for page in payload["pages"]
    )


def test_workspace_view_hides_another_users_run(client, second_client) -> None:
    project = create_project(client, "归属校验")
    run = create_run(client, project["id"], auto_start=False)
    register_user(second_client, "workspace-other@test.dev")

    response = second_client.get(f"{API}/task-runs/{run['id']}/workspace")
    assert response.status_code == 404


def test_openapi_preserves_shared_timestamp_component(app) -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert "Timestamp" in schemas
    assert schemas["Project"]["properties"]["created_at"]["$ref"] == (
        "#/components/schemas/Timestamp"
    )
    assert schemas["TaskRun"]["properties"]["created_at"]["$ref"] == (
        "#/components/schemas/Timestamp"
    )
    assert schemas["AgentEvent"]["properties"]["created_at"]["$ref"] == (
        "#/components/schemas/Timestamp"
    )
