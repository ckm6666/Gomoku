"""服务端配置。

配置全部来自环境变量；敏感 JWT secret 不得写入客户端。开发环境未提供 secret
时会生成一次性随机 secret（重启后旧 token 失效）；生产环境必须显式配置。
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from gomoku.shared.errors import ConfigurationError

logger = logging.getLogger(__name__)

_SERVER_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _SERVER_ROOT / "data" / "gomoku.db"


def _int_env(
    source: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = source.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} 必须在 {minimum}～{maximum} 之间"
        )
    return value


@dataclass(frozen=True)
class Settings:
    """一次服务进程使用的不可变配置。"""

    environment: str
    database_url: str
    jwt_secret: str
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    turn_seconds: int = 30
    ws_auth_timeout_seconds: float = 10.0
    ws_idle_timeout_seconds: float = 75.0
    max_ws_message_chars: int = 8192
    room_code_length: int = 8

    @property
    def is_development(self) -> bool:
        return self.environment.lower() in {"dev", "development", "test"}

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "Settings":
        source = os.environ if environ is None else environ
        environment = source.get("GOMOKU_SERVER_ENV", "development").strip() or "development"

        database_url = source.get("GOMOKU_DATABASE_URL", "").strip()
        if not database_url:
            _DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            database_url = (
                "sqlite+aiosqlite:///"
                + _DEFAULT_DB_PATH.as_posix()
            )

        jwt_secret = source.get("GOMOKU_JWT_SECRET", "").strip()
        if not jwt_secret:
            if environment.lower() == "production":
                raise ConfigurationError(
                    "生产环境必须设置 GOMOKU_JWT_SECRET"
                )
            jwt_secret = secrets.token_urlsafe(48)
            logger.warning(
                "未设置 GOMOKU_JWT_SECRET，开发环境使用一次性随机 secret；"
                "重启服务后旧 token 会失效"
            )
        if len(jwt_secret) < 32:
            raise ConfigurationError("GOMOKU_JWT_SECRET 至少需要 32 个字符")

        access_minutes = _int_env(
            source, "GOMOKU_ACCESS_TOKEN_MINUTES", 15, 1, 24 * 60
        )
        refresh_days = _int_env(
            source, "GOMOKU_REFRESH_TOKEN_DAYS", 30, 1, 365
        )
        turn_seconds = _int_env(
            source, "GOMOKU_TURN_SECONDS", 30, 5, 600
        )
        try:
            auth_timeout = float(
                source.get("GOMOKU_WS_AUTH_TIMEOUT", "10").strip()
            )
            idle_timeout = float(
                source.get("GOMOKU_WS_IDLE_TIMEOUT", "75").strip()
            )
        except ValueError as exc:
            raise ConfigurationError("WebSocket 超时必须是数字") from exc
        if auth_timeout <= 0 or auth_timeout > 60:
            raise ConfigurationError("GOMOKU_WS_AUTH_TIMEOUT 必须在 0～60 秒")
        if idle_timeout < 30 or idle_timeout > 3600:
            raise ConfigurationError("GOMOKU_WS_IDLE_TIMEOUT 必须在 30～3600 秒")

        return cls(
            environment=environment,
            database_url=database_url,
            jwt_secret=jwt_secret,
            access_token_minutes=access_minutes,
            refresh_token_days=refresh_days,
            turn_seconds=turn_seconds,
            ws_auth_timeout_seconds=auth_timeout,
            ws_idle_timeout_seconds=idle_timeout,
            room_code_length=8,
        )


__all__ = ["Settings"]
