"""注册邮箱验证码流程（D3）：发送/校验/作废/占用邮箱拒绝。"""
from __future__ import annotations


def test_register_requires_valid_code(client):
    email = "code-flow@test.dev"
    sent = client.post("/api/auth/register/send-code", json={"email": email})
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["ok"] is True and body["expires_in"] > 0
    code = body["dev_code"]  # 开发模式直返，生产走 SMTP 后不再返回

    wrong = client.post(
        "/api/auth/register",
        json={"email": email, "code": "000000", "password": "Passw0rd123", "name": "验证码用户"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "INVALID_EMAIL_CODE"

    ok = client.post(
        "/api/auth/register",
        json={"email": email, "code": code, "password": "Passw0rd123", "name": "验证码用户"},
    )
    assert ok.status_code == 201, ok.text

    # 验证码一次性：同码重放（换邮箱前提不成立，此处直接同邮箱二次注册）应 409
    again = client.post(
        "/api/auth/register",
        json={"email": email, "code": code, "password": "Passw0rd123", "name": "验证码用户"},
    )
    assert again.status_code == 409


def test_resend_invalidates_previous_code(client):
    email = "resend@test.dev"
    first = client.post("/api/auth/register/send-code", json={"email": email}).json()["dev_code"]
    second = client.post("/api/auth/register/send-code", json={"email": email}).json()["dev_code"]

    stale = client.post(
        "/api/auth/register",
        json={"email": email, "code": first, "password": "Passw0rd123", "name": "重发用户"},
    )
    assert stale.status_code == 401

    fresh = client.post(
        "/api/auth/register",
        json={"email": email, "code": second, "password": "Passw0rd123", "name": "重发用户"},
    )
    assert fresh.status_code == 201, fresh.text


def test_send_code_rejects_taken_email(client):
    me = client.get("/api/account/me").json()["user"]  # fixture 已注册用户
    response = client.post("/api/auth/register/send-code", json={"email": me["email"]})
    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_TAKEN"
