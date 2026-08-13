"""/api/system：设置中心「诊断」读取的后端运行时信息。"""

from __future__ import annotations


def test_system_info_is_public_and_non_sensitive(second_client):
    """未登录可读；只暴露方言名等事实，绝不能带出连接串或路径。"""
    response = second_client.get("/api/system")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["name"] == "OpenMathModel API"
    assert payload["version"] == "0.1.0"
    assert payload["database"] == "sqlite"
    assert payload["runner_enabled"] is False
    # 形如 3.10.11 的版本号与 ISO 时间戳
    assert all(part.isdigit() for part in payload["python"].split("."))
    assert "T" in payload["time"]

    serialized = response.text
    assert "://" not in serialized.replace('"time"', "")  # 无连接串
    assert "test.db" not in serialized  # 无文件路径
