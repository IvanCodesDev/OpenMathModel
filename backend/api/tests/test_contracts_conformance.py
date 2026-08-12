"""API 响应必须逐字段符合 packages/contracts/schemas/v1 —— Phase 1 的契约测试。"""

from __future__ import annotations

import importlib.util
import json

import pytest
from pydantic import ValidationError

from omm_contracts import ModelingWorkspaceView

from conftest import (
    API,
    REPO_ROOT,
    approve_when_asked,
    create_project,
    create_run,
    get_run,
    run_status_is,
    wait_until,
)


def _load_contracts():
    path = REPO_ROOT / "packages" / "contracts" / "validate.py"
    spec = importlib.util.spec_from_file_location("omm_contracts_validate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_api_payloads_conform_to_v1_contracts(client):
    contracts = _load_contracts()
    schemas = contracts.load_schemas("v1")

    project = create_project(client)
    assert contracts.validate_payload(schemas, "project", project) == []

    run = create_run(client, project["id"])
    run_id = run["id"]
    assert contracts.validate_payload(schemas, "task-run", run) == []

    approve_when_asked(client, run_id)
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))

    final = get_run(client, run_id)
    assert contracts.validate_payload(schemas, "task-run", final) == []

    steps = client.get(f"{API}/task-runs/{run_id}/steps").json()["items"]
    assert steps
    for step in steps:
        assert contracts.validate_payload(schemas, "step-run", step) == [], step

    events = client.get(f"{API}/task-runs/{run_id}/events/history").json()["items"]
    assert events
    for event in events:
        assert contracts.validate_payload(schemas, "agent-event", event) == [], event

    approvals = client.get(f"{API}/task-runs/{run_id}/approvals").json()["items"]
    assert approvals
    for approval in approvals:
        assert contracts.validate_payload(schemas, "approval-request", approval) == [], approval

    artifacts = client.get(f"{API}/projects/{project['id']}/artifacts").json()["items"]
    assert artifacts
    for artifact in artifacts:
        assert contracts.validate_payload(schemas, "artifact", artifact) == [], artifact

    error_body = client.get(f"{API}/projects/proj_" + "0" * 32).json()
    assert contracts.validate_payload(schemas, "error", error_body) == []


def test_contract_rejects_malformed_payload(client):
    """反向防呆：Schema 必须能拒绝坏数据，证明契约校验不是摆设。"""
    contracts = _load_contracts()
    schemas = contracts.load_schemas("v1")

    project = create_project(client)
    run = create_run(client, project["id"], auto_start=False)

    broken = dict(run)
    broken["status"] = "running"  # 小写非法
    assert contracts.validate_payload(schemas, "task-run", broken)

    broken = dict(run)
    broken["extra_internal_field"] = 1  # 意外泄漏内部字段
    assert contracts.validate_payload(schemas, "task-run", broken)


def test_workspace_contract_rejects_duplicate_page_keys():
    contracts = _load_contracts()
    schemas = contracts.load_schemas("v1")
    fixture = (
        REPO_ROOT
        / "packages"
        / "contracts"
        / "fixtures"
        / "v1"
        / "invalid"
        / "modeling-workspace-view.duplicate-pages.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    assert contracts.validate_payload(schemas, "modeling-workspace-view", payload)


def test_workspace_contract_rejects_inconsistent_agent_action():
    contracts = _load_contracts()
    schemas = contracts.load_schemas("v1")
    fixture = (
        REPO_ROOT
        / "packages"
        / "contracts"
        / "fixtures"
        / "v1"
        / "valid"
        / "modeling-workspace-view.1.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["agent"]["action"] = {
        "kind": "navigate",
        "label": "前往当前阶段",
        "target_route": "/workspace/model-plan",
        "approval_id": "appr_" + "3" * 32,
        "option_id": "approve",
    }

    assert contracts.validate_payload(schemas, "modeling-workspace-view", payload)
    with pytest.raises(ValidationError):
        ModelingWorkspaceView.model_validate(payload)
