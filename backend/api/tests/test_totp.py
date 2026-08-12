"""TOTP 实现正确性：RFC 6238 附录 B 官方测试向量 + 行为测试。"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omm_api.security import generate_totp_secret, totp_at, verify_totp

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


def test_rfc6238_vectors():
    assert totp_at(RFC_SECRET, 59, digits=8) == "94287082"
    assert totp_at(RFC_SECRET, 1111111109, digits=8) == "07081804"
    assert totp_at(RFC_SECRET, 1234567890, digits=8) == "89005924"
    assert totp_at(RFC_SECRET, 2000000000, digits=8) == "69279037"


def test_verify_current_code():
    secret = generate_totp_secret()
    code = totp_at(secret, time.time())
    assert verify_totp(secret, code)
    assert verify_totp(secret, f" {code[:3]} {code[3:]} ")  # 容忍空格


def test_verify_window_and_rejects():
    secret = generate_totp_secret()
    now = time.time()
    assert verify_totp(secret, totp_at(secret, now - 30))  # 上一个周期在窗口内
    assert verify_totp(secret, totp_at(secret, now + 30))  # 下一个周期在窗口内
    assert not verify_totp(secret, totp_at(secret, now - 120))  # 超出窗口
    assert not verify_totp(secret, "abc123")
    assert not verify_totp(secret, "12345")  # 位数不足
