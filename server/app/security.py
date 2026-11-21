"""密码与令牌辅助。"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import unicodedata
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from gomoku.shared.errors import ProtocolError
from server.app.config import Settings

_PASSWORD_HASHER = PasswordHasher()

#: 密码哈希/校验专用的线程池上限。
#:
#: Argon2id 默认每次校验要占约 64 MiB 内存，而 ``asyncio.to_thread`` 用的
#: 默认线程池按 CPU 数量放大到几十个 worker——一次并发登录洪泛就能把内存
#: 打满。用一个小的专用线程池把并发压住：登录失败再多也只是排队。
_VERIFY_MAX_WORKERS = 4
_HASH_MAX_WORKERS = 2

_verify_pool = ThreadPoolExecutor(
    max_workers=_VERIFY_MAX_WORKERS, thread_name_prefix="pw-verify"
)
_hash_pool = ThreadPoolExecutor(
    max_workers=_HASH_MAX_WORKERS, thread_name_prefix="pw-hash"
)


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
    """Argon2id 哈希放到专用线程池，避免阻塞 FastAPI 事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _hash_pool, _PASSWORD_HASHER.hash, password
    )


async def verify_password(password: str, password_hash: str) -> bool:
    """校验密码；并发被专用线程池限制，避免 Argon2 内存洪泛。"""

    def _verify() -> bool:
        try:
            return _PASSWORD_HASHER.verify(password_hash, password)
        except (VerifyMismatchError, ValueError):
            return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_verify_pool, _verify)


class LoginBackoff:
    """登录失败的渐进退避。

    与被移除的“失败次数锁定”不同，这里**不锁定账号也不返回 429**：
    正常用户输错几次密码照样能登录，只是响应慢一点，上限
    ``max_delay_seconds``。这样既不会把正常用户挡在门外，又能把在线
    暴力猜测的速率压到不可行的程度。

    计数按 ``用户名|IP`` 归组，成功登录后清零；闲置超过
    ``window_seconds`` 的分组会被顺带清理，避免字典无限增长。
    """

    def __init__(
        self,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 3.0,
        window_seconds: float = 300.0,
    ) -> None:
        self._base = base_delay_seconds
        self._max = max_delay_seconds
        self._window = window_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def delay_for(self, key: str) -> float:
        """返回本次请求应当等待的秒数，并顺带清理过期记录。"""
        now = time.monotonic()
        async with self._lock:
            self._prune(now)
            count = len(self._failures.get(key, ()))
        if count <= 0:
            return 0.0
        # 0.5s → 1s → 2s → 3s（封顶）
        return min(self._max, self._base * (2 ** (count - 1)))

    async def record_failure(self, key: str) -> None:
        """记一次失败。"""
        now = time.monotonic()
        async with self._lock:
            self._prune(now)
            self._failures[key].append(now)

    async def clear(self, key: str) -> None:
        """登录成功后清零计数。"""
        async with self._lock:
            self._failures.pop(key, None)

    def _prune(self, now: float) -> None:
        """丢掉超过窗口期的失败记录。"""
        stale = [
            key
            for key, stamps in self._failures.items()
            if not stamps or now - stamps[-1] > self._window
        ]
        for key in stale:
            self._failures.pop(key, None)


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


__all__ = [
    "LoginBackoff",
    "normalize_username",
    "validate_username",
    "validate_password",
    "hash_password",
    "verify_password",
    "sha256_hex",
    "generate_refresh_token",
    "create_access_token",
    "decode_access_token",
]
