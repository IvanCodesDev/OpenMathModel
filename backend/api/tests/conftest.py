from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from omm_api.config import Settings
from omm_api.main import create_app

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
# 契约事实来源：schemas/v1（老板仲裁：v1 为准）
SCHEMA_DIR = REPO_ROOT / "packages" / "contracts" / "schemas" / "v1"

API = "/api/v1"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = [
        (path.name, Resource.from_contents(_load(path)))
        for path in SCHEMA_DIR.glob("*.schema.json")
    ]
    return Registry().with_resources(resources)


@pytest.fixture()
def app(tmp_path: Path):
    # OMM_TEST_DATABASE_URL：对真实 PostgreSQL 实跑同一套件（用完 drop_all 清理）
    override = os.environ.get("OMM_TEST_DATABASE_URL")
    settings = Settings(
        database_url=override or f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        runner_enabled=False,
        retention_sweep_enabled=False,
        # 预热线程会真实导入 paddle 栈（开发机可能装了），测试必须关闭保证确定性
        vl_warmup_enabled=False,
        sse_poll_seconds=0.01,
        sse_heartbeat_seconds=60.0,
        # 与本地 backend/api/.env 隔离：测试永远走开发模式验证码，绝不真实发信
        smtp_host="",
        email_dev_mode=True,
        artifacts_dir=tmp_path / "artifacts",
        avatars_dir=tmp_path / "avatars",
    )
    application = create_app(settings)
    yield application
    if override:
        application.state.db.drop_all()
        application.state.db.dispose()


def register_user(
    test_client: TestClient,
    email: str,
    password: str = "Passw0rd123",
    name: str = "测试用户",
) -> dict:
    """注册（含邮箱验证码步骤；开发模式下验证码随发送响应返回）。"""
    sent = test_client.post("/api/auth/register/send-code", json={"email": email})
    assert sent.status_code == 200, sent.text
    code = sent.json()["dev_code"]
    response = test_client.post(
        "/api/auth/register",
        json={"email": email, "code": code, "password": password, "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["user"]


@pytest.fixture()
def client(app):
    """默认已登录客户端：/api/v1 资源自 C2 起要求登录（Cookie 会话）。

    认证类测试在用例内自行注册/登录会覆盖此默认会话，互不影响。
    """
    with TestClient(app) as test_client:
        register_user(test_client, f"fixture-{uuid4().hex[:12]}@test.dev")
        yield test_client


@pytest.fixture()
def second_client(app):
    """同一应用、独立 Cookie 罐：模拟另一台设备/另一个用户（不预登录）。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def validate_contract():
    registry = _registry()

    def _validate(schema_name: str, payload: dict) -> None:
        schema = _load(SCHEMA_DIR / schema_name)
        Draft202012Validator(
            schema, registry=registry, format_checker=FormatChecker()
        ).validate(payload)

    return _validate


# ── 可导入的测试助手（tick 驱动，确定性推进） ──────────────────────────


def create_project(client: TestClient, name: str = "契约测试项目") -> dict:
    response = client.post(f"{API}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def create_run(client: TestClient, project_id: str, goal: str = "完成基线建模", **kwargs) -> dict:
    body = {"project_id": project_id, "goal": goal, **kwargs}
    response = client.post(f"{API}/task-runs", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def get_run(client: TestClient, run_id: str) -> dict:
    return client.get(f"{API}/task-runs/{run_id}").json()


def run_status_is(client: TestClient, run_id: str, status: str) -> Callable[[], Optional[dict]]:
    def probe() -> Optional[dict]:
        payload = get_run(client, run_id)
        return payload if payload.get("status") == status else None

    return probe


def pending_approval(client: TestClient, run_id: str) -> Callable[[], Optional[dict]]:
    def probe() -> Optional[dict]:
        items = client.get(f"{API}/task-runs/{run_id}/approvals").json()["items"]
        matching = [a for a in items if a["status"] == "PENDING"]
        return matching[0] if matching else None

    return probe


def wait_until(
    client: TestClient, run_id: str, condition: Callable[[], Optional[Any]], max_ticks: int = 60
) -> Any:
    """tick 驱动的确定性等待：先探测，不满足则推进一步，直到条件满足。"""
    for _ in range(max_ticks):
        result = condition()
        if result:
            return result
        client.app.state.advancer.advance(run_id)
    raise AssertionError(f"condition not met after {max_ticks} ticks")


def approve_when_asked(
    client: TestClient, run_id: str, option_id: Optional[str] = None, **extra
) -> dict:
    approval = wait_until(client, run_id, pending_approval(client, run_id))
    body: dict[str, Any] = {"action": "approve", "approval_id": approval["id"], **extra}
    if option_id:
        body["option_id"] = option_id
    response = client.post(f"{API}/task-runs/{run_id}/actions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def make_project(client: TestClient):
    def _make(name: str = "测试项目") -> dict:
        return create_project(client, name)

    return _make


@pytest.fixture()
def make_run(client: TestClient, make_project):
    def _make(goal: str = "完成基线建模", **kwargs) -> dict:
        project = make_project()
        body = {"auto_start": False, **kwargs}
        return create_run(client, project["id"], goal=goal, **body)

    return _make


@pytest.fixture()
def tick(client: TestClient):
    """手动驱动推进器（测试关闭后台线程，逐 tick 断言确定性状态）。"""

    def _tick(run_id: str, times: int = 1) -> str | None:
        status = None
        for _ in range(times):
            status = client.app.state.advancer.advance(run_id)
        return status

    return _tick
