import pytest

from omm_agent_tools import TaskWorkspace, WorkspaceArtifactStore, WorkspaceViolation


@pytest.fixture()
def workspace(tmp_path):
    return TaskWorkspace(root=tmp_path, run_id="run_x", quota_bytes=1024)


def test_write_read_roundtrip_and_listing(workspace):
    workspace.write_text("data/input.csv", "a,b\n1,2\n")
    assert workspace.read_text("data/input.csv") == "a,b\n1,2\n"
    assert workspace.list_files() == ["data/input.csv"]
    assert workspace.exists("data/input.csv")
    assert not workspace.exists("data/missing.csv")


def test_parent_traversal_is_rejected(workspace):
    with pytest.raises(WorkspaceViolation):
        workspace.resolve("../escape.txt")
    with pytest.raises(WorkspaceViolation):
        workspace.write_text("data/../../escape.txt", "nope")


def test_absolute_paths_are_rejected(workspace, tmp_path):
    with pytest.raises(WorkspaceViolation):
        workspace.resolve(str(tmp_path / "outside.txt"))


def test_exists_never_leaks_outside_paths(workspace):
    assert workspace.exists("../../etc/passwd") is False


def test_quota_is_enforced(workspace):
    workspace.write_bytes("big.bin", b"x" * 1000)
    with pytest.raises(WorkspaceViolation):
        workspace.write_bytes("more.bin", b"y" * 100)


def test_root_itself_resolves(workspace):
    assert workspace.resolve(".") == workspace.root


def test_artifact_store_addresses_and_hashes_content(workspace):
    store = WorkspaceArtifactStore(workspace)
    ref = store.put(
        run_id="run_x",
        kind="table",
        name="../sneaky/metrics.json",  # directory part must be stripped
        content=b'{"rmse": 0.12}',
        media_type="application/json",
        producer_step="step_1",
    )
    assert ref.size == len(b'{"rmse": 0.12}')
    assert len(ref.sha256) == 64
    assert ref.producer_step == "step_1"
    assert "sneaky" not in ref.uri
    from pathlib import Path

    stored = Path(ref.uri)
    assert stored.exists()
    assert workspace.root in stored.parents
