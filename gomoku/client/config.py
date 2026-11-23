"""客户端运行配置。

当前单机/AI 功能不需要网络配置；这些字段为未来 OnlineSession 预留，
通过环境变量注入，不把服务器地址或任何密钥硬编码进 exe。配置中不包含
密码、access token 或 refresh token。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

from gomoku.shared.errors import ConfigurationError

#: 打包后（PyInstaller）默认连接的公网入口，由 Tailscale Funnel 代理本机
#: 8000 端口。源码运行时仍走开发默认（127.0.0.1:8000），环境变量可覆盖。
DEFAULT_API_BASE_URL = "https://gomoku-host.taild407ad.ts.net"
DEFAULT_WS_BASE_URL = "wss://gomoku-host.taild407ad.ts.net/api/v1/ws"


def _is_frozen() -> bool:
    """是否为 PyInstaller 打包产物。"""
    return bool(getattr(sys, "frozen", False))

_VALID_LOG_LEVELS = {
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
}


def _optional_url(value: str, schemes: set[str], field: str) -> Optional[str]:
    """校验并规范化可选的 HTTP/WS 服务地址。"""
    value = value.strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.netloc:
        allowed = "/".join(sorted(schemes))
        raise ConfigurationError(f"{field} 必须是 {allowed} URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class AppConfig:
    """应用运行时的非敏感配置。"""

    environment: str = "development"
    api_base_url: Optional[str] = None
    ws_base_url: Optional[str] = None
    request_timeout_seconds: float = 10.0
    log_level: str = "INFO"

    @property
    def online_configured(self) -> bool:
        """是否已同时配置 REST 与 WebSocket 地址。"""
        return bool(self.api_base_url and self.ws_base_url)

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "AppConfig":
        """从环境变量读取配置；不读取本地密钥文件。"""
        source = os.environ if environ is None else environ

        api_url = _optional_url(
            source.get("GOMOKU_API_BASE_URL", ""),
            {"https", "http"},
            "GOMOKU_API_BASE_URL",
        )
        ws_url = _optional_url(
            source.get("GOMOKU_WS_BASE_URL", ""),
            {"wss", "ws"},
            "GOMOKU_WS_BASE_URL",
        )

        # 打包产物默认连固定公网入口，朋友拿到 exe 无需配置环境变量；
        # 未打包时保持离线/本机开发默认，避免开发误连线上。
        if not api_url and not ws_url and _is_frozen():
            api_url = DEFAULT_API_BASE_URL
            ws_url = DEFAULT_WS_BASE_URL

        log_level = source.get("GOMOKU_LOG_LEVEL", "INFO").strip().upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigurationError(
                "GOMOKU_LOG_LEVEL 必须是 DEBUG/INFO/WARNING/ERROR/CRITICAL"
            )

        raw_timeout = source.get("GOMOKU_REQUEST_TIMEOUT", "10").strip()
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ConfigurationError(
                "GOMOKU_REQUEST_TIMEOUT 必须是正数"
            ) from exc
        if timeout <= 0:
            raise ConfigurationError("GOMOKU_REQUEST_TIMEOUT 必须大于 0")

        return cls(
            environment=source.get("GOMOKU_ENV", "development").strip()
            or "development",
            api_base_url=api_url,
            ws_base_url=ws_url,
            request_timeout_seconds=timeout,
            log_level=log_level,
        )


__all__ = [
    "AppConfig",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_WS_BASE_URL",
]
