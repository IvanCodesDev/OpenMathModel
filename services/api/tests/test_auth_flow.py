"""认证与账户安全端到端流程测试（TestClient + SQLite 临时库）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _register(client, email, password="Passw0rd123", name="测试用户"):
    sent = client.post("/api/auth/register/send-code", json={"email": email})
    # 已注册邮箱在发送阶段即 409；带占位验证码走注册以拿到同语义响应
    code = sent.json().get("dev_code", "000000") if sent.status_code == 200 else "000000"
    return client.post(
        "/api/auth/register",
        json={"email": email, "code": code, "password": password, "name": name},
    )


def test_register_me_and_duplicate(client):
    response = _register(client, "alice@example.com")
    assert response.status_code == 201
    assert response.json()["user"]["email"] == "alice@example.com"

    me = client.get("/api/account/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["name"] == "测试用户"
    assert body["security"]["two_factor_enabled"] is False

    duplicate = _register(client, "Alice@Example.com")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "EMAIL_TAKEN"


def test_password_rules_and_login_failure(client):
    weak = _register(client, "weak@example.com", password="short")
    assert weak.status_code == 422
    assert weak.json()["code"] == "VALIDATION_ERROR"

    _register(client, "bob@example.com")
    client.post("/api/auth/logout")

    wrong = client.post("/api/auth/login", json={"email": "bob@example.com", "password": "WrongPass1"})
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "INVALID_CREDENTIALS"

    ok = client.post("/api/auth/login", json={"email": "bob@example.com", "password": "Passw0rd123"})
    assert ok.status_code == 200
    assert ok.json()["two_factor_required"] is False


def test_profile_update(client):
    _register(client, "carol@example.com")

    rename = client.patch("/api/account/profile", json={"name": "Carol"})
    assert rename.status_code == 200
    assert rename.json()["user"]["name"] == "Carol"

    no_password = client.patch("/api/account/profile", json={"email": "carol2@example.com"})
    assert no_password.status_code == 400
    assert no_password.json()["code"] == "PASSWORD_REQUIRED"

    wrong_password = client.patch(
        "/api/account/profile", json={"email": "carol2@example.com", "password": "WrongPass1"}
    )
    assert wrong_password.status_code == 401

    ok = client.patch(
        "/api/account/profile", json={"email": "carol2@example.com", "password": "Passw0rd123"}
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == "carol2@example.com"


def test_change_password_revokes_other_sessions(client, second_client):
    _register(client, "dave@example.com")
    login2 = second_client.post(
        "/api/auth/login", json={"email": "dave@example.com", "password": "Passw0rd123"}
    )
    assert login2.status_code == 200
    assert second_client.get("/api/account/me").status_code == 200

    wrong = client.post(
        "/api/account/password",
        json={"current_password": "WrongPass1", "new_password": "NewPassw0rd456"},
    )
    assert wrong.status_code == 401

    changed = client.post(
        "/api/account/password",
        json={"current_password": "Passw0rd123", "new_password": "NewPassw0rd456"},
    )
    assert changed.status_code == 200
    assert changed.json()["revoked_sessions"] == 1

    # 当前设备保持登录，其他设备被退出
    assert client.get("/api/account/me").status_code == 200
    assert second_client.get("/api/account/me").status_code == 401

    relogin = second_client.post(
        "/api/auth/login", json={"email": "dave@example.com", "password": "NewPassw0rd456"}
    )
    assert relogin.status_code == 200


def test_two_factor_full_cycle(client):
    from omm_api.security import totp_at

    _register(client, "erin@example.com")

    setup = client.get("/api/account/2fa/setup")
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")

    bad_enable = client.post("/api/account/2fa/enable", json={"code": "000000"})
    assert bad_enable.status_code == 401

    enable = client.post("/api/account/2fa/enable", json={"code": totp_at(secret, time.time())})
    assert enable.status_code == 200
    recovery_codes = enable.json()["recovery_codes"]
    assert len(recovery_codes) == 10

    me = client.get("/api/account/me").json()
    assert me["security"]["two_factor_enabled"] is True
    assert me["security"]["recovery_codes_remaining"] == 10

    # 退出后重新登录需要第二步验证
    client.post("/api/auth/logout")
    assert client.get("/api/account/me").status_code == 401

    login = client.post("/api/auth/login", json={"email": "erin@example.com", "password": "Passw0rd123"})
    assert login.status_code == 200
    assert login.json()["two_factor_required"] is True
    challenge = login.json()["challenge_token"]

    bad_code = client.post("/api/auth/login/2fa", json={"challenge_token": challenge, "code": "000000"})
    assert bad_code.status_code == 401

    ok = client.post(
        "/api/auth/login/2fa",
        json={"challenge_token": challenge, "code": totp_at(secret, time.time())},
    )
    assert ok.status_code == 200
    assert client.get("/api/account/me").status_code == 200

    # 恢复代码：可用一次，不可复用
    client.post("/api/auth/logout")
    login = client.post("/api/auth/login", json={"email": "erin@example.com", "password": "Passw0rd123"})
    challenge = login.json()["challenge_token"]
    recovery_login = client.post(
        "/api/auth/login/2fa", json={"challenge_token": challenge, "code": recovery_codes[0]}
    )
    assert recovery_login.status_code == 200
    assert client.get("/api/account/me").json()["security"]["recovery_codes_remaining"] == 9

    client.post("/api/auth/logout")
    login = client.post("/api/auth/login", json={"email": "erin@example.com", "password": "Passw0rd123"})
    challenge = login.json()["challenge_token"]
    reuse = client.post(
        "/api/auth/login/2fa", json={"challenge_token": challenge, "code": recovery_codes[0]}
    )
    assert reuse.status_code == 401

    # 重新生成恢复代码 → 剩余数量重置
    fresh = client.post(
        "/api/auth/login/2fa",
        json={"challenge_token": challenge, "code": totp_at(secret, time.time())},
    )
    assert fresh.status_code == 200
    regen = client.post("/api/account/2fa/recovery-codes", json={"password": "Passw0rd123"})
    assert regen.status_code == 200
    assert len(regen.json()["recovery_codes"]) == 10
    assert client.get("/api/account/me").json()["security"]["recovery_codes_remaining"] == 10

    # 关闭双重验证
    disable = client.post("/api/account/2fa/disable", json={"password": "Passw0rd123"})
    assert disable.status_code == 200
    me = client.get("/api/account/me").json()
    assert me["security"]["two_factor_enabled"] is False
    assert me["security"]["recovery_codes_remaining"] == 0


def test_sessions_management(client, second_client):
    _register(client, "frank@example.com")
    login2 = second_client.post(
        "/api/auth/login", json={"email": "frank@example.com", "password": "Passw0rd123"}
    )
    assert login2.status_code == 200

    listing = client.get("/api/account/sessions").json()["sessions"]
    assert len(listing) == 2
    current = [s for s in listing if s["current"]]
    others = [s for s in listing if not s["current"]]
    assert len(current) == 1 and len(others) == 1

    cannot_self = client.delete(f"/api/account/sessions/{current[0]['id']}")
    assert cannot_self.status_code == 400

    revoke = client.delete(f"/api/account/sessions/{others[0]['id']}")
    assert revoke.status_code == 200
    assert second_client.get("/api/account/me").status_code == 401

    # 再登录一台设备，用退出其他设备一键清理
    second_client.post("/api/auth/login", json={"email": "frank@example.com", "password": "Passw0rd123"})
    revoke_others = client.post("/api/account/sessions/revoke-others")
    assert revoke_others.status_code == 200
    assert revoke_others.json()["revoked_sessions"] == 1
    assert second_client.get("/api/account/me").status_code == 401


def test_origin_check_blocks_foreign_site(client):
    blocked = client.post(
        "/api/auth/login",
        json={"email": "alice@example.com", "password": "Passw0rd123"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "ORIGIN_FORBIDDEN"

    allowed_origin = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert allowed_origin.status_code == 200


def test_login_rate_limit_last(client):
    """放在最后：限速器按邮箱+IP 记录失败，避免污染其他用例。

    C4 起限速器为数据库实现（多实例一致），经 app.state.login_limiter 注入。
    """
    _register(client, "grace@example.com")
    client.post("/api/auth/logout")

    saw_429 = False
    for _ in range(12):
        response = client.post(
            "/api/auth/login", json={"email": "grace@example.com", "password": "WrongPass1"}
        )
        if response.status_code == 429:
            saw_429 = True
            break
    assert saw_429

    client.app.state.login_limiter.reset(["ip:testclient", "email:grace@example.com"])
