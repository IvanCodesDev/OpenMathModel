"""账户头像：上传、读取、移除与格式/体积护栏。"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import register_user  # noqa: E402
from omm_api.config import Settings  # noqa: E402
from omm_api.main import create_app  # noqa: E402

# 1×1 透明 PNG；只有魔数参与识别，但用真实图片更贴近浏览器上传
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _upload(client: TestClient, content: bytes, filename: str, media_type: str):
    return client.post("/api/account/avatar", files={"file": (filename, content, media_type)})


def test_upload_serves_content_and_exposes_versioned_url(client):
    assert client.get("/api/account/me").json()["user"]["avatar_url"] is None

    uploaded = _upload(client, PNG_BYTES, "avatar.png", "image/png")
    assert uploaded.status_code == 200, uploaded.text
    avatar_url = uploaded.json()["user"]["avatar_url"]
    assert avatar_url is not None and avatar_url.startswith("/api/account/avatar?v=")
    assert client.get("/api/account/me").json()["user"]["avatar_url"] == avatar_url

    served = client.get("/api/account/avatar")
    assert served.status_code == 200
    assert served.content == PNG_BYTES
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["cache-control"] == "private, max-age=86400"


def test_changing_avatar_changes_cache_busting_url(client):
    first = _upload(client, PNG_BYTES, "a.png", "image/png").json()["user"]["avatar_url"]
    second = _upload(client, JPEG_BYTES, "b.jpg", "image/jpeg").json()["user"]["avatar_url"]
    assert first != second
    assert client.get("/api/account/avatar").headers["content-type"] == "image/jpeg"


def test_declared_content_type_cannot_smuggle_non_image(client):
    """声明为 PNG 的 SVG 必须被拒；否则同源返回可执行内容等同于脚本注入。"""
    rejected = _upload(client, SVG_BYTES, "avatar.png", "image/png")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "UNSUPPORTED_IMAGE"
    assert client.get("/api/account/me").json()["user"]["avatar_url"] is None


def test_empty_upload_is_rejected(client):
    empty = _upload(client, b"", "avatar.png", "image/png")
    assert empty.status_code == 422
    assert empty.json()["code"] == "VALIDATION_ERROR"


def test_remove_is_idempotent_and_falls_back_to_letter(client):
    _upload(client, PNG_BYTES, "avatar.png", "image/png")

    removed = client.delete("/api/account/avatar")
    assert removed.status_code == 200
    assert removed.json()["user"]["avatar_url"] is None
    assert removed.json()["user"]["avatar_letter"]
    assert client.get("/api/account/avatar").status_code == 404

    again = client.delete("/api/account/avatar")
    assert again.status_code == 200
    assert again.json()["user"]["avatar_url"] is None


def test_avatar_endpoints_require_login(second_client):
    assert second_client.get("/api/account/avatar").status_code == 401
    assert _upload(second_client, PNG_BYTES, "avatar.png", "image/png").status_code == 401
    assert second_client.delete("/api/account/avatar").status_code == 401


def test_other_account_avatar_is_not_reachable(client, second_client, tmp_path):
    """头像只按当前会话返回：另一账户读到的是自己的空状态，而不是他人头像。"""
    _upload(client, PNG_BYTES, "avatar.png", "image/png")
    register_user(second_client, "avatar-neighbour@test.dev")
    assert second_client.get("/api/account/avatar").status_code == 404
    assert second_client.get("/api/account/me").json()["user"]["avatar_url"] is None


@pytest.fixture()
def small_limit_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'avatar-limit.db').as_posix()}",
        runner_enabled=False,
        # 与本地 backend/api/.env 隔离：key 清空即关闭远程 OCR，测试绝不外呼
        ocr_api_key="",
        smtp_host="",
        email_dev_mode=True,
        artifacts_dir=tmp_path / "artifacts",
        avatars_dir=tmp_path / "avatars",
        avatar_max_bytes=512,
    )
    with TestClient(create_app(settings)) as test_client:
        register_user(test_client, "avatar-limit@test.dev")
        yield test_client


def test_oversized_avatar_is_rejected(small_limit_client):
    oversized = PNG_BYTES + b"\x00" * 1024
    response = _upload(small_limit_client, oversized, "big.png", "image/png")
    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert small_limit_client.get("/api/account/me").json()["user"]["avatar_url"] is None
