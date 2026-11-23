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
    # 对局中掉线后保留重连的宽限时间；超时仍未回来则判对手获胜（逃跑）。
    disconnect_forfeit_seconds: float = 20.0
    max_ws_message_chars: int = 8192
    room_code_length: int = 8
    # 日志级别与格式（text/json）；生产建议 json 便于采集。
    log_level: str = "INFO"
    log_format: str = "text"
    # 公平性与资源限制。
    undo_limit_per_game: int = 3
    undo_cooldown_seconds: float = 10.0
    max_spectators_per_room: int = 50
    max_connections_per_ip: int = 20
    ws_command_burst: int = 40
    ws_command_window_seconds: float = 10.0
    # 仅在服务端位于可信反向代理之后时开启，用于读取 X-Forwarded-For。
    trust_proxy_headers: bool = False
    # 排位赛季长度（天）与管理员账号（可复核举报/查看回放审计）。
    season_length_days: int = 90
    admin_usernames: frozenset[str] = frozenset()

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
            disconnect_forfeit = float(
                source.get(
                    "GOMOKU_DISCONNECT_FORFEIT_SECONDS", "20"
                ).strip()
            )
        except ValueError as exc:
            raise ConfigurationError("WebSocket 超时必须是数字") from exc
        if auth_timeout <= 0 or auth_timeout > 60:
            raise ConfigurationError("GOMOKU_WS_AUTH_TIMEOUT 必须在 0～60 秒")
        if idle_timeout < 30 or idle_timeout > 3600:
            raise ConfigurationError("GOMOKU_WS_IDLE_TIMEOUT 必须在 30～3600 秒")
        if disconnect_forfeit <= 0 or disconnect_forfeit > 300:
            raise ConfigurationError(
                "GOMOKU_DISCONNECT_FORFEIT_SECONDS 必须在 0～300 秒"
            )

        log_level = source.get("GOMOKU_LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError(
                "GOMOKU_LOG_LEVEL 必须是 DEBUG/INFO/WARNING/ERROR/CRITICAL"
            )
        log_format = source.get("GOMOKU_LOG_FORMAT", "text").strip().lower()
        if log_format not in {"text", "json"}:
            raise ConfigurationError("GOMOKU_LOG_FORMAT 必须是 text 或 json")

        undo_limit = _int_env(
            source, "GOMOKU_UNDO_LIMIT_PER_GAME", 3, 0, 20
        )
        max_spectators = _int_env(
            source, "GOMOKU_MAX_SPECTATORS_PER_ROOM", 50, 0, 1000
        )
        max_connections = _int_env(
            source, "GOMOKU_MAX_CONNECTIONS_PER_IP", 20, 1, 1000
        )
        ws_burst = _int_env(
            source, "GOMOKU_WS_COMMAND_BURST", 40, 1, 1000
        )
        try:
            undo_cooldown = float(
                source.get("GOMOKU_UNDO_COOLDOWN_SECONDS", "10").strip()
            )
            ws_window = float(
                source.get(
                    "GOMOKU_WS_COMMAND_WINDOW_SECONDS", "10"
                ).strip()
            )
        except ValueError as exc:
            raise ConfigurationError("悔棋/命令限流参数必须是数字") from exc
        if undo_cooldown < 0 or undo_cooldown > 600:
            raise ConfigurationError(
                "GOMOKU_UNDO_COOLDOWN_SECONDS 必须在 0～600 秒"
            )
        if ws_window < 1 or ws_window > 3600:
            raise ConfigurationError(
                "GOMOKU_WS_COMMAND_WINDOW_SECONDS 必须在 1～3600 秒"
            )
        season_days = _int_env(
            source, "GOMOKU_SEASON_LENGTH_DAYS", 90, 1, 3650
        )
        admin_raw = source.get("GOMOKU_ADMIN_USERNAMES", "")
        admin_usernames = frozenset(
            item.strip().lower()
            for item in admin_raw.split(",")
            if item.strip()
        )
        trust_raw = source.get(
            "GOMOKU_TRUST_PROXY_HEADERS", "0"
        ).strip().lower()
        if trust_raw in {"1", "true", "yes", "on"}:
            trust_proxy_headers = True
        elif trust_raw in {"", "0", "false", "no", "off"}:
            trust_proxy_headers = False
        else:
            raise ConfigurationError(
                "GOMOKU_TRUST_PROXY_HEADERS 必须是 0/1/true/false"
            )

        return cls(
            environment=environment,
            database_url=database_url,
            jwt_secret=jwt_secret,
            access_token_minutes=access_minutes,
            refresh_token_days=refresh_days,
            turn_seconds=turn_seconds,
            ws_auth_timeout_seconds=auth_timeout,
            ws_idle_timeout_seconds=idle_timeout,
            disconnect_forfeit_seconds=disconnect_forfeit,
            room_code_length=8,
            log_level=log_level,
            log_format=log_format,
            undo_limit_per_game=undo_limit,
            undo_cooldown_seconds=undo_cooldown,
            max_spectators_per_room=max_spectators,
            max_connections_per_ip=max_connections,
            ws_command_burst=ws_burst,
            ws_command_window_seconds=ws_window,
            trust_proxy_headers=trust_proxy_headers,
            season_length_days=season_days,
            admin_usernames=admin_usernames,
        )


__all__ = ["Settings"]
