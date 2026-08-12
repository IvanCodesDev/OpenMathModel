from __future__ import annotations

from sqlalchemy import select

from omm_api.orm import ApprovalRequestRow


def _action(client, run_id: str, action: str, **extra):
    return client.post(f"/api/v1/task-runs/{run_id}/actions", json={"action": action, **extra})


def _node(client, run_id: str) -> str:
    return client.get(f"/api/v1/task-runs/{run_id}").json()["current_node"]


def test_full_run_reaches_approval_then_completes(client, make_run, tick, validate_contract):
    run = make_run("单摆建模基线")
    run_id = run["id"]
    assert run["status"] == "QUEUED"
    assert run["current_node"] == "CREATED"
    validate_contract("task-run.schema.json", run)

    # 引擎语义：一拍完成一个阶段（题意解析 → 数据准备 → 建模方案+请求确认）
    assert tick(run_id) == "RUNNING"
    assert _node(client, run_id) == "PROBLEM_ANALYSIS"
    assert tick(run_id) == "RUNNING"
    assert _node(client, run_id) == "DATA_PREPARATION"
    assert tick(run_id) == "WAITING_APPROVAL"
    assert _node(client, run_id) == "MODEL_PLANNING"

    approvals = client.get(f"/api/v1/task-runs/{run_id}/approvals").json()["items"]
    assert len(approvals) == 1
    assert approvals[0]["status"] == "PENDING"
    validate_contract("approval-request.schema.json", approvals[0])

    approved = _action(client, run_id, "approve", option_id="approve", comment="采用当前方案")
    assert approved.status_code == 200
    assert approved.json()["status"] == "RUNNING"
    assert approved.json()["current_node"] == "EXPERIMENTING"  # 实验阶段已同拍完成

    assert tick(run_id) == "RUNNING"
    assert _node(client, run_id) == "VALIDATING"
    assert tick(run_id) == "RUNNING"
    assert _node(client, run_id) == "PAPER_WRITING"
    assert tick(run_id) == "COMPLETED"

    final = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert final["status"] == "COMPLETED"
    assert final["current_node"] == "COMPLETED"
    assert final["ended_at"] is not None
    validate_contract("task-run.schema.json", final)

    steps = client.get(f"/api/v1/task-runs/{run_id}/steps").json()["items"]
    assert [s["status"] for s in steps] == ["SUCCEEDED"] * 6
    assert all(s["input_hash"] for s in steps)
    validate_contract("step-run.schema.json", steps[0])

    artifacts = client.get(f"/api/v1/projects/{final['project_id']}/artifacts").json()
    kinds = sorted(a["kind"] for a in artifacts["items"])
    assert kinds == ["figure", "report"]
    validate_contract("artifact.schema.json", artifacts["items"][0])


def test_approve_invalid_outside_waiting_approval(client, make_run, validate_contract):
    run = make_run()
    response = _action(client, run["id"], "approve")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "INVALID_ACTION"
    validate_contract("error.schema.json", body)


def test_approve_is_idempotent_with_client_token(client, make_run, tick):
    run = make_run()
    run_id = run["id"]
    tick(run_id, times=4)  # 到 WAITING_APPROVAL

    first = _action(client, run_id, "approve", client_token="tok_same_1")
    assert first.status_code == 200
    assert first.json()["status"] == "RUNNING"

    second = _action(client, run_id, "approve", client_token="tok_same_1")
    assert second.status_code == 200
    assert second.json()["status"] == "RUNNING"

    # 无令牌的重复 approve 是非法动作
    third = _action(client, run_id, "approve")
    assert third.status_code == 409


def test_approve_requires_option_id_for_multiple_positive_candidates(
    client, app, make_run, tick, validate_contract
):
    run = make_run()
    run_id = run["id"]
    tick(run_id, times=3)  # 到 WAITING_APPROVAL

    with app.state.db.session_factory() as session:
        pending = session.execute(
            select(ApprovalRequestRow).where(
                ApprovalRequestRow.run_id == run_id,
                ApprovalRequestRow.status == "PENDING",
            )
        ).scalar_one()
        pending.options = [
            {"id": "plan-a", "label": "方案 A"},
            {"id": "plan-b", "label": "方案 B"},
            {"id": "reject", "label": "退回重做"},
        ]
        session.commit()

    missing = _action(client, run_id, "approve")
    assert missing.status_code == 409
    body = missing.json()
    assert body["code"] == "CONFLICT"
    assert body["message"] == "审批包含多个候选方案，必须明确提供 option_id"
    assert body["details"] == {
        "required": "option_id",
        "options": ["plan-a", "plan-b", "reject"],
    }
    validate_contract("error.schema.json", body)

    # 冲突请求不消费审批；客户端补齐选择后可以继续。
    approvals = client.get(f"/api/v1/task-runs/{run_id}/approvals").json()["items"]
    assert approvals[0]["status"] == "PENDING"
    chosen = _action(client, run_id, "approve", option_id="plan-b")
    assert chosen.status_code == 200
    assert chosen.json()["status"] == "RUNNING"


def test_pause_resume_cycle(client, make_run, tick):
    run = make_run()
    run_id = run["id"]
    tick(run_id, times=2)  # RUNNING，DATA_PREPARATION 进行中

    paused = _action(client, run_id, "pause")
    assert paused.json()["status"] == "PAUSED"

    assert tick(run_id) == "PAUSED"  # 暂停后 tick 不推进

    # pause 幂等
    assert _action(client, run_id, "pause").json()["status"] == "PAUSED"

    resumed = _action(client, run_id, "resume")
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["current_node"] == "DATA_PREPARATION"


def test_cancel_terminates_run_and_steps(client, make_run, tick):
    run = make_run()
    run_id = run["id"]
    tick(run_id, times=2)

    cancelled = _action(client, run_id, "cancel")
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["ended_at"] is not None

    # 引擎步骤同拍完成，取消时不存在悬挂中的步骤；已有步骤保持终态
    steps = client.get(f"/api/v1/task-runs/{run_id}/steps").json()["items"]
    assert steps and all(s["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"} for s in steps)

    assert tick(run_id) == "CANCELLED"  # 终态不再推进
    assert _action(client, run_id, "resume").status_code == 409
    assert _action(client, run_id, "cancel").status_code == 200  # 幂等


def test_injected_failure_then_retry_completes(client, make_run, tick):
    run = make_run("实验容错验证 [fail:experiment]")
    run_id = run["id"]
    tick(run_id, times=3)  # 到 WAITING_APPROVAL
    approved = _action(client, run_id, "approve")  # 默认第一个选项 = approve
    assert approved.status_code == 200
    # 审批后同拍执行 EXPERIMENTING：失败注入在第 1 次尝试生效
    assert approved.json()["status"] == "FAILED"

    failed = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert failed["failure"]["failure_class"] == "CODE_DEFECT"
    assert failed["failure"]["message"]

    retried = _action(client, run_id, "retry")
    assert retried.json()["status"] == "RUNNING"
    assert retried.json()["failure"] is None

    assert tick(run_id) == "RUNNING"  # 第 2 次尝试执行并成功
    tick(run_id, times=3)  # 验证 → 论文 → 完成
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "COMPLETED"

    steps = client.get(f"/api/v1/task-runs/{run_id}/steps").json()["items"]
    experiment_steps = [s for s in steps if s["node"] == "EXPERIMENTING"]
    assert [(s["attempt"], s["status"]) for s in experiment_steps] == [
        (1, "FAILED"),
        (2, "SUCCEEDED"),
    ]


def test_create_run_for_unknown_project(client):
    response = client.post(
        "/api/v1/task-runs", json={"project_id": "proj_missing000000", "goal": "x"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_unknown_action_rejected_by_validation(client, make_run):
    run = make_run()
    response = _action(client, run["id"], "explode")
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
