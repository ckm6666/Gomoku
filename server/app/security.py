"""密码、令牌与登录限流。"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from gomoku.shared.errors import ProtocolError
from server.app.config import Settings

_PASSWORD_HASHER = PasswordHasher()


def normalize_username(raw: str) -> tuple[str, str]:
    """返回规范化显示名和用于唯一性比较的 key。"""
    display = unicodedata.normalize("NFKC", raw).strip()
    key = display.casefold()
    return display, key


def validate_username(raw: str) -> bool:
    display, _ = normalize_username(raw)
    if not 2 <= len(display) <= 24:
        return False
    return all(ch.isalnum() or ch == "_" for ch in display)


def validate_password(password: str) -> bool:
    return isinstance(password, str) and 8 <= len(password) <= 128


async def hash_password(password: str) -> str:
    """Argon2id 哈希放到线程池，避免阻塞 FastAPI 事件循环。"""
    return await asyncio.to_thread(_PASSWORD_HASHER.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    def _verify() -> bool:
        try:
            return _PASSWORD_HASHER.verify(password_hash, password)
        except (VerifyMismatchError, ValueError):
            return False

    return await asyncio.to_thread(_verify)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_refresh_token() -> tuple[str, str]:
    """返回 (明文 refresh token, 入库哈希)。明文只返回给客户端一次。"""
    raw = secrets.token_urlsafe(48)
    return raw, sha256_hex(raw)


def create_access_token(
    user_id: str,
    settings: Settings,
) -> tuple[str, int]:
    """返回 (JWT access token, 有效秒数)。"""
    now = int(time.time())
    expires_in = settings.access_token_minutes * 60
    payload = {
        "sub": user_id,
        "type": "access",
        "iat": now,
        "exp": now + expires_in,
        "jti": str(uuid.uuid4()),
        "iss": "gomoku-server",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, expires_in


def decode_access_token(token: str, settings: Settings) -> str:
    """校验 access token 并返回 user_id；非法时抛 ProtocolError。"""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise ProtocolError("访问令牌无效或已过期") from exc

    if payload.get("type") != "access" or payload.get("iss") != "gomoku-server":
        raise ProtocolError("访问令牌类型不正确")
    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise ProtocolError("访问令牌缺少用户")
    return user_id


@dataclass
class LoginThrottleDecision:
    allowed: bool
    retry_after_seconds: int = 0


class LoginThrottle:
    """小型单进程登录失败限流器。

    生产多实例时应迁移到 Redis；当前 Stage 1 单 worker 足够阻止明显的
    在线暴力猜测。
    """

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 300,
        lockout_seconds: int = 60,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._locked_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> LoginThrottleDecision:
        now = time.monotonic()
        async with self._lock:
            locked_until = self._locked_until.get(key, 0.0)
            if locked_until > now:
                return LoginThrottleDecision(
                    False, max(1, int(locked_until - now + 0.999))
                )
            recent = [
                stamp
                for stamp in self._failures.get(key, [])
                if now - stamp <= self._window
            ]
            self._failures[key] = recent
            if len(recent) >= self._max_failures:
                self._locked_until[key] = now + self._lockout
                return LoginThrottleDecision(False, self._lockout)
            return LoginThrottleDecision(True)

    async def record_failure(self, key: str) -> None:
        now = time.monotonic()
        async with self._lock:
            recent = [
                stamp
                for stamp in self._failures.get(key, [])
                if now - stamp <= self._window
            ]
            recent.append(now)
            self._failures[key] = recent
            if len(recent) >= self._max_failures:
                self._locked_until[key] = now + self._lockout

    async def clear(self, key: str) -> None:
        async with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


__all__ = [
    "normalize_username",
    "validate_username",
    "validate_password",
    "hash_password",
    "verify_password",
    "sha256_hex",
    "generate_refresh_token",
    "create_access_token",
    "decode_access_token",
    "LoginThrottle",
    "LoginThrottleDecision",
]
