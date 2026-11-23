"""客户端运行配置。

当前单机/AI 功能不需要网络配置；这些字段为未来 OnlineSession 预留。
配置来源有两处：环境变量优先，其次是打包时固化进 exe 的内置值
（见 :mod:`gomoku.client.build_info`）。配置中不包含密码、access token
或 refresh token。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

from gomoku.client.build_info import load_build_info
from gomoku.client.updater import default_manifest_url
from gomoku.shared.errors import ConfigurationError

_VALID_LOG_LEVELS = {
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
}

#: 布尔型环境变量允许的写法（大小写不敏感）。
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


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


def _optional_bool(value: str, field: str, default: bool) -> bool:
    """解析布尔型环境变量；空值使用默认值，非法值报错。"""
    text = value.strip().lower()
    if not text:
        return default
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{field} 必须是 1/0 或 true/false、yes/no、on/off"
    )


@dataclass(frozen=True)
class AppConfig:
    """应用运行时的非敏感配置。"""

    environment: str = "development"
    api_base_url: Optional[str] = None
    ws_base_url: Optional[str] = None
    request_timeout_seconds: float = 10.0
    log_level: str = "INFO"
    update_manifest_url: Optional[str] = None
    auto_check_updates: bool = True

    @property
    def online_configured(self) -> bool:
        """是否已同时配置 REST 与 WebSocket 地址。"""
        return bool(self.api_base_url and self.ws_base_url)

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "AppConfig":
        """读取配置；环境变量优先，其次是打包时固化的内置值。

        优先级（环境变量始终压过内置值）：

        * ``api_base_url``：``GOMOKU_API_BASE_URL`` → 内置服务地址 → 无；
        * ``update_manifest_url``：``GOMOKU_UPDATE_MANIFEST_URL`` →
          内置清单地址 → 按服务地址推导 → 无；
        * ``auto_check_updates``：``GOMOKU_AUTO_UPDATE_CHECK`` →
          内置开关 → 默认开启。

        不读取本地密钥文件。
        """
        source = os.environ if environ is None else environ
        baked = load_build_info()

        api_url = _optional_url(
            source.get("GOMOKU_API_BASE_URL", ""),
            {"https", "http"},
            "GOMOKU_API_BASE_URL",
        )
        if api_url is None:
            api_url = _optional_url(
                baked.api_base_url,
                {"https", "http"},
                "内置服务地址",
            )
        ws_url = _optional_url(
            source.get("GOMOKU_WS_BASE_URL", ""),
            {"wss", "ws"},
            "GOMOKU_WS_BASE_URL",
        )

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

        manifest_url = _optional_url(
            source.get("GOMOKU_UPDATE_MANIFEST_URL", ""),
            {"https", "http"},
            "GOMOKU_UPDATE_MANIFEST_URL",
        )
        if manifest_url is None:
            manifest_url = _optional_url(
                baked.update_manifest_url,
                {"https", "http"},
                "内置更新清单地址",
            )
        # 未显式指定清单地址时，按联机服务地址推导自家服务器的更新接口。
        if manifest_url is None:
            manifest_url = default_manifest_url(api_url)
        baked_auto = (
            True if baked.auto_update_check is None else baked.auto_update_check
        )
        auto_check_updates = _optional_bool(
            source.get("GOMOKU_AUTO_UPDATE_CHECK", ""),
            "GOMOKU_AUTO_UPDATE_CHECK",
            default=baked_auto,
        )

        return cls(
            environment=source.get("GOMOKU_ENV", "development").strip()
            or "development",
            api_base_url=api_url,
            ws_base_url=ws_url,
            request_timeout_seconds=timeout,
            log_level=log_level,
            update_manifest_url=manifest_url,
            auto_check_updates=auto_check_updates,
        )


__all__ = ["AppConfig"]
