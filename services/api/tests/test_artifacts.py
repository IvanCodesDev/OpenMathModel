"""Artifact 存储闭环（B4）：上传/下载/哈希核验/归属隔离/模拟产物真实落盘。"""

from __future__ import annotations

import hashlib

from conftest import API, approve_when_asked, create_project, create_run, register_user, run_status_is, wait_until


def _upload(client, project_id: str, content: bytes, filename: str, kind: str = "dataset", **extra):
    return client.post(
        f"{API}/projects/{project_id}/artifacts",
        files={"file": (filename, content, "text/csv")},
        data={"kind": kind, **extra},
    )


def test_upload_download_roundtrip_with_hash(client, validate_contract):
    project = create_project(client)
    content = "t,y\n0,1\n1,2\n".encode("utf-8")
    expected_sha = hashlib.sha256(content).hexdigest()

    response = _upload(client, project["id"], content, "data.csv")
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["sha256"] == expected_sha
    assert artifact["size_bytes"] == len(content)
    assert artifact["kind"] == "dataset"
    assert artifact["status"] == "READY"
    validate_contract("artifact.schema.json", artifact)

    download = client.get(f"{API}/artifacts/{artifact['id']}/download")
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["x-content-sha256"] == expected_sha
    assert "attachment" in download.headers["content-disposition"]


def test_upload_validations(client):
    project = create_project(client)

    bad_kind = _upload(client, project["id"], b"x", "a.bin", kind="nonsense")
    assert bad_kind.status_code == 422

    empty = _upload(client, project["id"], b"", "empty.bin")
    assert empty.status_code == 422

    unknown_run = _upload(client, project["id"], b"x", "a.bin", run_id="run_" + "0" * 32)
    assert unknown_run.status_code == 404


def test_download_is_owner_scoped(client, second_client, app):
    project = create_project(client)
    uploaded = _upload(client, project["id"], b"secret-bytes", "s.bin").json()

    register_user(second_client, "intruder@test.dev")
    stolen = second_client.get(f"{API}/artifacts/{uploaded['id']}/download")
    assert stolen.status_code == 404
    assert stolen.json()["code"] == "NOT_FOUND"


def test_sim_run_artifacts_are_downloadable(client):
    project = create_project(client)
    run = create_run(client, project["id"])
    run_id = run["id"]
    approve_when_asked(client, run_id)
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))

    artifacts = client.get(f"{API}/projects/{project['id']}/artifacts").json()["items"]
    kinds = sorted(a["kind"] for a in artifacts)
    assert kinds == ["figure", "report"]
    for artifact in artifacts:
        download = client.get(f"{API}/artifacts/{artifact['id']}/download")
        assert download.status_code == 200, download.text
        assert hashlib.sha256(download.content).hexdigest() == artifact["sha256"]


def test_corrupted_blob_is_refused(client, app):
    project = create_project(client)
    content = b"will-be-corrupted"
    uploaded = _upload(client, project["id"], content, "c.bin").json()

    sha = uploaded["sha256"]
    blob_path = app.state.settings.artifacts_dir / sha[:2] / sha[2:4] / sha
    blob_path.write_bytes(b"tampered!")

    download = client.get(f"{API}/artifacts/{uploaded['id']}/download")
    assert download.status_code == 500
    assert download.json()["code"] == "ARTIFACT_CORRUPTED"
