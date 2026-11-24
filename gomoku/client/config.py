"""客户端运行配置。

当前单机/AI 功能不需要网络配置；这些字段为联机功能预留。地址优先从环境变量
读取；打包产物（PyInstaller）在未配置任何地址时回落到固定的公网入口，源码运行
仍走开发默认（本机 127.0.0.1:8000）。硬编码的只有服务器地址，不含任何密钥；
配置中不包含密码、access token 或 refresh token。

唯一可选的凭据是 ``GOMOKU_GITHUB_TOKEN``：只用于「检查更新」时提升 GitHub
API 的匿名频率限制，不参与任何游戏逻辑，也不会被写进日志。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

from gomoku.client.frozen import is_frozen
from gomoku.shared.errors import ConfigurationError

#: 打包后（PyInstaller）默认连接的公网入口，由 Tailscale Funnel 代理维护者
#: 本机的 8000 端口。源码运行时仍走开发默认（127.0.0.1:8000），环境变量可覆盖。
_PUBLIC_API_BASE_URL = "https://gomoku-host.taild407ad.ts.net"
#: 与 GOMOKU_WS_BASE_URL 同语义：只含 scheme + host，WS 路径由客户端拼接。
_PUBLIC_WS_BASE_URL = "wss://gomoku-host.taild407ad.ts.net"

_API_TO_WS_SCHEME = {"https": "wss", "http": "ws"}
_WS_TO_API_SCHEME = {"wss": "https", "ws": "http"}

#: 服务端接口的版本号；升级到 v2 时只改这一处。
API_VERSION = "v1"

#: 所有 REST 接口的统一路径前缀。
API_PREFIX = f"/api/{API_VERSION}"


def api_path(*parts: str) -> str:
    """把路径片段拼成带版本前缀的接口路径。

    斜杠会被统一规范化，调用方写 ``"auth/login"`` 或 ``"/auth/login"``
    得到的结果完全一致::

        api_path("auth", "login")   # "/api/v1/auth/login"
        api_path("/auth/login")     # "/api/v1/auth/login"

    升级接口版本时只需要改 :data:`API_VERSION`，不必逐个文件改字符串。
    """
    segments = [part.strip("/") for part in parts if part and part.strip("/")]
    if not segments:
        return API_PREFIX
    return f"{API_PREFIX}/" + "/".join(segments)


#: 服务端 WebSocket 路径；客户端只需要主机部分，路径在这里统一拼接。
WS_PATH = f"{API_PREFIX}/ws"

#: 开发环境在未配置地址时使用的本机服务端地址。
DEV_API_BASE_URL = "http://127.0.0.1:8000"

_DEV_ENVIRONMENTS = {"development", "dev", "test", "local"}

_VALID_LOG_LEVELS = {
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
}


def _optional_url(
    value: str,
    schemes: set[str],
    field: str,
) -> Optional[str]:
    """校验并规范化可选的 HTTP/WS 服务地址。

    地址只允许形如 ``scheme://host:port``：带了路径的话，客户端拼上
    ``/api/v1/...`` 之后会得到一个服务端根本不提供的地址，属于配置错误，
    这里直接拒绝并点名是哪个环境变量填错了。
    """
    value = value.strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.netloc:
        allowed = "/".join(sorted(schemes))
        raise ConfigurationError(f"{field} 必须是 {allowed} URL")
    if parsed.path.strip("/"):
        raise ConfigurationError(
            f"{field} 不能包含路径（当前为 {parsed.path}），"
            "只能填写形如 scheme://host:port 的地址"
        )
    return value.rstrip("/")


# 内置入口与用户填写的地址走同一套校验，避免拼写错误一路带到所有分发产物里。
DEFAULT_API_BASE_URL = (
    _optional_url(_PUBLIC_API_BASE_URL, {"https", "http"}, "DEFAULT_API_BASE_URL")
    or ""
)
DEFAULT_WS_BASE_URL = (
    _optional_url(_PUBLIC_WS_BASE_URL, {"wss", "ws"}, "DEFAULT_WS_BASE_URL") or ""
)


def derive_ws_base_url(api_base_url: str) -> Optional[str]:
    """由 REST 地址推导同源 WebSocket 基址（https→wss、http→ws）。"""
    parsed = urlparse(api_base_url)
    scheme = _API_TO_WS_SCHEME.get(parsed.scheme)
    if scheme is None or not parsed.netloc:
        return None
    return f"{scheme}://{parsed.netloc}"


def derive_api_base_url(ws_base_url: str) -> Optional[str]:
    """由 WebSocket 基址推导同源 REST 地址（wss→https、ws→http）。"""
    parsed = urlparse(ws_base_url)
    scheme = _WS_TO_API_SCHEME.get(parsed.scheme)
    if scheme is None or not parsed.netloc:
        return None
    return f"{scheme}://{parsed.netloc}"


@dataclass(frozen=True)
class AppConfig:
    """应用运行时的非敏感配置。"""

    environment: str = "development"
    api_base_url: Optional[str] = None
    ws_base_url: Optional[str] = None
    request_timeout_seconds: float = 10.0
    log_level: str = "INFO"
    #: 可选的 GitHub token，仅用于「检查更新」时放宽 API 频率限制。
    github_token: str = ""

    def __post_init__(self) -> None:
        """补齐联机地址：打包产物回落公网入口，单侧配置按同源推导另一侧。

        只配置了 REST 或只配置了 WebSocket 时，按同源推导另一侧
        （`https`↔`wss`、`http`↔`ws`），避免出现「自定义 REST + 公网 WS」
        这类前后端指向不同入口的组合。打包产物（PyInstaller）在完全没配置
        地址时回落到固定的公网入口，朋友拿到 exe 无需配置环境变量；源码运行
        保持离线/本机开发默认，避免开发误连线上。
        """
        api_url = self.api_base_url
        ws_url = self.ws_base_url
        if not api_url and not ws_url:
            if is_frozen():
                api_url = DEFAULT_API_BASE_URL
                ws_url = DEFAULT_WS_BASE_URL
        elif api_url and not ws_url:
            ws_url = derive_ws_base_url(api_url)
        elif ws_url and not api_url:
            api_url = derive_api_base_url(ws_url)

        if api_url != self.api_base_url:
            object.__setattr__(self, "api_base_url", api_url)
        if ws_url != self.ws_base_url:
            object.__setattr__(self, "ws_base_url", ws_url)

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

        # 打包产物默认按生产语义运行，避免日志把正式分发包标成 development。
        default_environment = "production" if is_frozen() else "development"
        return cls(
            environment=source.get("GOMOKU_ENV", default_environment).strip()
            or default_environment,
            api_base_url=api_url,
            ws_base_url=ws_url,
            request_timeout_seconds=timeout,
            log_level=log_level,
            # 可选：只为「检查更新」放宽 GitHub API 的匿名频率限制。
            github_token=source.get("GOMOKU_GITHUB_TOKEN", "").strip(),
        )


def resolve_endpoints(config: AppConfig) -> tuple[str, str]:
    """解析实际使用的 REST 与 WebSocket 地址。

    返回 ``(api_base_url, ws_url)``：WebSocket 地址已带上 ``/api/v1/ws`` 路径。
    开发环境在完全没有配置地址时回落到本机服务端；打包产物此时已经带上内置
    的公网入口，不会再回落到本机。

    WS 基址带路径时直接报错：拼出来的 ``wss://host/foo/api/v1/ws`` 服务端
    从来不提供，报配置错误比让连接莫名其妙失败要好。
    """
    api = (config.api_base_url or "").rstrip("/")
    ws_base = (config.ws_base_url or "").rstrip("/")

    is_dev = config.environment.lower() in _DEV_ENVIRONMENTS
    if not api and is_dev:
        api = DEV_API_BASE_URL
    if not ws_base and api:
        ws_base = derive_ws_base_url(api) or ""
    if not ws_base:
        return api, ""
    if ws_base.endswith(WS_PATH):
        return api, ws_base
    if urlparse(ws_base).path.strip("/"):
        raise ConfigurationError(
            "GOMOKU_WS_BASE_URL 不能包含路径，"
            f"只能是形如 wss://host:port 的地址（当前为 {ws_base}）"
        )
    return api, ws_base + WS_PATH


__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "AppConfig",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_WS_BASE_URL",
    "DEV_API_BASE_URL",
    "WS_PATH",
    "api_path",
    "derive_api_base_url",
    "derive_ws_base_url",
    "resolve_endpoints",
]
