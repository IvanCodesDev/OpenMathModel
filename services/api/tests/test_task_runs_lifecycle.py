from __future__ import annotations

from conftest import (
    API,
    approve_when_asked,
    create_project,
    create_run,
    get_run,
    pending_approval,
    run_status_is,
    wait_until,
)

SIM_NODES = [
    "PROBLEM_ANALYSIS",
    "DATA_PREPARATION",
    "MODEL_PLANNING",
    "EXPERIMENTING",
    "VALIDATING",
    "PAPER_WRITING",
]


def _events(client, run_id: str) -> list[dict]:
    return client.get(f"{API}/task-runs/{run_id}/events/history").json()["items"]


def test_happy_path_completes_with_consistent_timeline(client):
    project = create_project(client)
    run = create_run(client, project["id"])
    run_id = run["id"]
    assert run["status"] == "QUEUED"
    assert run["current_node"] == "CREATED"

    approve_when_asked(client, run_id)
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))

    final = get_run(client, run_id)
    assert final["current_node"] == "COMPLETED"
    assert final["started_at"] is not None
    assert final["ended_at"] is not None
    assert final["failure"] is None

    steps = client.get(f"{API}/task-runs/{run_id}/steps").json()["items"]
    succeeded_nodes = [s["node"] for s in steps if s["status"] == "SUCCEEDED"]
    assert succeeded_nodes == SIM_NODES
    assert all(s["attempt"] == 1 for s in steps)
    assert all(s["input_hash"] for s in steps)

    events = _events(client, run_id)
    sequences = [e["sequence"] for e in events]
    assert sequences == list(range(1, len(events) + 1)), "sequence 必须连续且无重复"
    types = [e["type"] for e in events]
    assert types[0] == "run.created"
    assert "approval.requested" in types
    assert "approval.resolved" in types
    assert "run.node_changed" in types
    assert {"from": "WAITING_APPROVAL", "to": "RUNNING", "reason": "方案已确认"} in [
        e["payload"] for e in events if e["type"] == "run.status_changed"
    ]
    assert events[-1]["type"] == "run.status_changed"
    assert events[-1]["payload"]["to"] == "COMPLETED"


def test_reject_reruns_planning_and_asks_again(client):
    project = create_project(client)
    run = create_run(client, project["id"])
    run_id = run["id"]

    first = approve_when_asked(client, run_id, option_id="reject")
    # 拒绝后同拍重做 MODEL_PLANNING（attempt 2）并再次请求确认
    assert first["status"] == "WAITING_APPROVAL"
    assert first["current_node"] == "MODEL_PLANNING"

    steps = client.get(f"{API}/task-runs/{run_id}/steps").json()["items"]
    planning_attempts = [s["attempt"] for s in steps if s["node"] == "MODEL_PLANNING"]
    assert max(planning_attempts, default=0) == 2

    approval = wait_until(client, run_id, pending_approval(client, run_id))
    response = client.post(
        f"{API}/task-runs/{run_id}/actions",
        json={"action": "approve", "approval_id": approval["id"], "option_id": "approve"},
    )
    assert response.status_code == 200
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))

    approvals = client.get(f"{API}/task-runs/{run_id}/approvals").json()["items"]
    assert len(approvals) == 2
    assert all(a["status"] == "RESOLVED" for a in approvals)


def test_pause_freezes_progress_and_resume_continues(client):
    project = create_project(client)
    run = create_run(client, project["id"])
    run_id = run["id"]

    wait_until(client, run_id, run_status_is(client, run_id, "RUNNING"))
    response = client.post(f"{API}/task-runs/{run_id}/actions", json={"action": "pause"})
    assert response.status_code == 200
    assert response.json()["status"] == "PAUSED"

    count_before = len(_events(client, run_id))
    client.app.state.advancer.advance(run_id)
    client.app.state.advancer.advance(run_id)
    assert len(_events(client, run_id)) == count_before, "暂停期间不应产生新事件"

    response = client.post(f"{API}/task-runs/{run_id}/actions", json={"action": "resume"})
    assert response.status_code == 200

    approve_when_asked(client, run_id)
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))


def test_cancel_is_terminal_and_cancels_pending_approval(client):
    project = create_project(client)
    run = create_run(client, project["id"])
    run_id = run["id"]

    wait_until(client, run_id, pending_approval(client, run_id))
    response = client.post(f"{API}/task-runs/{run_id}/actions", json={"action": "cancel"})
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"

    approvals = client.get(f"{API}/task-runs/{run_id}/approvals").json()["items"]
    assert approvals and approvals[0]["status"] == "CANCELLED"

    final = get_run(client, run_id)
    assert final["status"] == "CANCELLED"
    assert final["ended_at"] is not None


def test_fail_injection_then_retry_completes(client):
    project = create_project(client)
    run = create_run(
        client,
        project["id"],
        params={"fail_at": "EXPERIMENTING", "fail_attempts": 1},
    )
    run_id = run["id"]

    approve_when_asked(client, run_id)
    wait_until(client, run_id, run_status_is(client, run_id, "FAILED"))

    failed = get_run(client, run_id)
    assert failed["failure"]["failure_class"] == "CODE_DEFECT"
    assert failed["failure"]["message"]
    types = [e["type"] for e in _events(client, run_id)]
    assert "step.failed" in types

    response = client.post(f"{API}/task-runs/{run_id}/actions", json={"action": "retry"})
    assert response.status_code == 200
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))

    steps = client.get(f"{API}/task-runs/{run_id}/steps").json()["items"]
    experimenting = [s for s in steps if s["node"] == "EXPERIMENTING"]
    assert [s["status"] for s in experimenting] == ["FAILED", "SUCCEEDED"]
    assert [s["attempt"] for s in experimenting] == [1, 2]
    assert get_run(client, run_id)["failure"] is None
