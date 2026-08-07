"""认证安全原语：密码哈希、会话令牌、TOTP（RFC 6238）、恢复代码、限速、UA 解析。

TOTP 使用标准库实现（HMAC-SHA1 / 30s / 6 位），与主流验证器 App 兼容，
测试用例中以 RFC 6238 附录 B 的官方向量验证正确性。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import threading
import time
from typing import Iterable, Optional

import bcrypt

from .config import PASSWORD_MAX_BYTES, SECRET_KEY

# ── 密码哈希 ─────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > PASSWORD_MAX_BYTES:
        raise ValueError("password too long")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > PASSWORD_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode("ascii"))
    except ValueError:
        return False


# ── 会话令牌 ─────────────────────────────────────────────────────


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ── 2FA 登录挑战令牌（无状态 HMAC 签名） ─────────────────────────


def sign_challenge(user_id: str, ttl_seconds: int) -> str:
    expires = int(time.time()) + ttl_seconds
    nonce = secrets.token_urlsafe(8)
    payload = f"{user_id}.{expires}.{nonce}"
    signature = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_challenge(token: str) -> Optional[str]:
    """校验挑战令牌，返回 user_id；无效或过期返回 None。"""
    parts = token.split(".")
    if len(parts) != 4:
        return None
    user_id, expires_text, nonce, signature = parts
    payload = f"{user_id}.{expires_text}.{nonce}"
    expected = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        if int(expires_text) < time.time():
            return None
    except ValueError:
        return None
    return user_id


# ── TOTP（RFC 6238） ─────────────────────────────────────────────


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp_at(secret: str, at_time: float, digits: int = 6, period: int = 30) -> str:
    return _hotp(secret, int(at_time // period), digits)


def verify_totp(secret: str, code: str, window: int = 1, period: int = 30) -> bool:
    normalized = code.strip().replace(" ", "")
    if not re.fullmatch(r"\d{6}", normalized):
        return False
    now = time.time()
    return any(
        hmac.compare_digest(totp_at(secret, now + step * period), normalized)
        for step in range(-window, window + 1)
    )


def build_otpauth_uri(secret: str, email: str, issuer: str = "OpenMathModel") -> str:
    from urllib.parse import quote

    label = quote(f"{issuer}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


# ── 注册邮箱验证码 ───────────────────────────────────────────────


def generate_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_email_code(email: str, code: str) -> str:
    normalized = code.strip().replace(" ", "")
    return sha256_hex(f"email-code:{email.strip().lower()}:{normalized}")


# ── 恢复代码 ─────────────────────────────────────────────────────

_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_recovery_codes(count: int) -> list[str]:
    codes = []
    for _ in range(count):
        chars = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{chars[:5]}-{chars[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", code.strip().upper())


def hash_recovery_code(code: str) -> str:
    return sha256_hex(f"recovery:{normalize_recovery_code(code)}")


# ── 登录限速（进程内） ───────────────────────────────────────────


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        cutoff = now - self.window_seconds
        attempts = [t for t in self._attempts.get(key, []) if t > cutoff]
        if attempts:
            self._attempts[key] = attempts
        else:
            self._attempts.pop(key, None)

    def allow(self, keys: Iterable[str]) -> bool:
        now = time.time()
        with self._lock:
            for key in keys:
                self._prune(key, now)
                if len(self._attempts.get(key, [])) >= self.max_attempts:
                    return False
        return True

    def record_failure(self, keys: Iterable[str]) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                self._attempts.setdefault(key, []).append(now)

    def reset(self, keys: Iterable[str]) -> None:
        with self._lock:
            for key in keys:
                self._attempts.pop(key, None)


# ── User-Agent 解析（够用即可，不引第三方依赖） ──────────────────


def _browser_version(ua: str, token: str) -> str:
    """提取 `token/主版本号`，找不到返回空串。"""
    match = re.search(re.escape(token) + r"/(\d+)", ua)
    return match.group(1) if match else ""


def parse_user_agent(user_agent: str) -> tuple[str, str, str]:
    """返回 (browser, os_name, kind)；browser 携带主版本号（如 Chrome 138）。"""
    ua = user_agent or ""

    if "Windows NT" in ua:
        os_name = "Windows"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "未知系统"

    if "Edg/" in ua or "Edge/" in ua:
        browser, version = "Edge", _browser_version(ua, "Edg") or _browser_version(ua, "Edge")
    elif "OPR/" in ua:
        browser, version = "Opera", _browser_version(ua, "OPR")
    elif "Firefox/" in ua:
        browser, version = "Firefox", _browser_version(ua, "Firefox")
    elif "Chrome/" in ua:
        browser, version = "Chrome", _browser_version(ua, "Chrome")
    elif "Safari/" in ua:
        browser, version = "Safari", _browser_version(ua, "Version")
    elif ua.startswith("python-httpx") or ua.startswith("curl"):
        browser, version = "API 客户端", ""
    else:
        browser, version = "未知浏览器", ""

    if version:
        browser = f"{browser} {version}"

    kind = "mobile" if os_name in {"iOS", "Android"} else "desktop"
    return browser, os_name, kind


def device_label(browser: str, os_name: str) -> str:
    return f"{browser} on {os_name}"
