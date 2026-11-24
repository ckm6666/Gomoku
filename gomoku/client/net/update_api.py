"""更新检查：从 GitHub Releases API 异步获取版本元数据。

版本信息只来自 GitHub Releases API：客户端自身**不提供**安装包下载入口，
安装包仍由用户从发布页手动获取，因此这里只负责读取 JSON 元数据并交给
界面层判断版本。

两层分工：
- 本模块（网络层）只发请求、解码 JSON、映射错误码；
- 界面层负责比较版本号并在浏览器里打开发布页。

日志里只允许出现状态码与错误枚举：令牌和响应正文都不许落盘。

注意：更新检查走的是 GitHub 官方 API，不是自建服务端，因此地址是完整的
绝对 URL，也没有 Bearer 头的强制要求（令牌只在 api.github.com 时附带）。
公共的 JSON 解析、错误提取与 reply 生命周期仍由
:class:`~gomoku.client.net.json_api.JsonApiClient` 统一处理。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from gomoku.client.constants import APP_VERSION, GITHUB_RELEASE_API_URL
from gomoku.client.net.json_api import JsonApiClient, ReplyOutcome
from gomoku.shared.update_check import ReleaseInfo, parse_release_payload

logger = logging.getLogger(__name__)

# 统一的错误文案：界面直接展示，这里不再二次加工。
_ERR_IN_PROGRESS = "更新检查正在进行中"
_ERR_BAD_URL = "更新地址无效"
_ERR_BAD_PAYLOAD = "更新信息格式异常，请稍后再试"
_ERR_RATE_LIMITED = "GitHub 请求频率受限，请稍后再试"
_ERR_NOT_FOUND = "未找到发布版本"
_ERR_NETWORK = "无法连接 GitHub，请检查网络"
_ERR_UNEXPECTED = "更新检查失败，请稍后再试"


class UpdateHTTPClient(JsonApiClient):
    """向 GitHub Releases API 查询最新版本；同一时刻只允许一个在途请求。"""

    release_ready = Signal(object)  # ReleaseInfo
    failed = Signal(str)  # 中文错误消息
    busy_changed = Signal(bool)

    invalid_url_message = _ERR_BAD_URL
    malformed_payload_message = _ERR_BAD_PAYLOAD
    default_error_message = _ERR_UNEXPECTED

    def __init__(
        self,
        api_url: str = GITHUB_RELEASE_API_URL,
        timeout_seconds: float = 10.0,
        token: str = "",
        *,
        manager: Optional[QNetworkAccessManager] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(
            api_url, timeout_seconds, parent=parent, manager=manager
        )
        # 令牌只用于提高 GitHub API 的频率上限；绝不入日志。
        self._token = token or ""
        self._reply: Optional[QNetworkReply] = None

    @property
    def busy(self) -> bool:
        return self._reply is not None

    # ------------------------------------------------------------------
    # 公开请求
    # ------------------------------------------------------------------
    def check(self) -> bool:
        """发起一次版本查询。

        返回 ``True`` 表示请求已发出；返回 ``False`` 表示根本没发出去，
        失败原因通过 ``failed`` 信号告知界面。
        """
        if self.busy:
            self.failed.emit(_ERR_IN_PROGRESS)
            return False

        url, error = self.prepare_url(self.base_url)
        if url is None:
            self.failed.emit(error)
            return False

        request = self.build_request(url, headers=self._github_headers(url))
        try:
            reply = self.manager.get(request)
        except RuntimeError:
            # 宿主窗口已销毁，事件循环已经退出。
            self.failed.emit(_ERR_NETWORK)
            return False

        self._reply = reply
        reply.finished.connect(lambda reply=reply: self._on_finished(reply))
        self.busy_changed.emit(True)
        return True

    def abort(self) -> None:
        """取消在途请求：不再发出 ``release_ready``，只复位忙状态。"""
        reply = self._reply
        if reply is None:
            return
        # 先摘掉引用再 abort：真实 Qt 会在 abort 后发出 finished，
        # 那时这条响应已经过期，不能再触发任何结果信号。
        self._reply = None
        try:
            reply.abort()
        except RuntimeError:
            pass
        self.busy_changed.emit(False)

    # ------------------------------------------------------------------
    # 请求/响应
    # ------------------------------------------------------------------
    def prepare_url(self, path: str) -> tuple[Optional[QUrl], str]:
        """更新检查直连 GitHub：地址是完整的绝对 URL，不拼基址。"""
        text = (path or "").strip()
        if not text:
            return None, self.invalid_url_message
        url = QUrl(text)
        if url.scheme() not in ("http", "https") or not url.host():
            return None, self.invalid_url_message
        return url, ""

    def is_success(
        self, status_code: Optional[int], network_error: bool
    ) -> bool:
        """更新检查必须有 2xx 状态码，而且 Qt 没有报网络错误。"""
        return (
            status_code is not None
            and 200 <= status_code < 300
            and not network_error
        )

    def _github_headers(self, url: QUrl) -> dict[str, bytes]:
        """构造 GitHub 约定的请求头。"""
        headers = {
            "Accept": b"application/vnd.github+json",
            "X-GitHub-Api-Version": b"2022-11-28",
            "User-Agent": f"Gomoku/{APP_VERSION}".encode("utf-8"),
        }
        # 令牌只发给 GitHub 官方 API：api_url 是可配置的，万一被改成第三方
        # 地址，把个人访问令牌带过去等于把凭据交出去。
        if self._token and url.host().lower() == "api.github.com":
            headers["Authorization"] = (
                f"Bearer {self._token}".encode("utf-8")
            )
        return headers

    def _on_finished(self, reply: QNetworkReply) -> None:
        """处理一次请求的结束。"""
        if reply is not self._reply:
            # 已经被 abort() 取消的旧响应：静默丢弃，不发任何信号。
            self.discard(reply)
            return

        self._reply = None
        # 先复位忙状态，界面就不会在提示框弹出时还显示“检查中”。
        self.busy_changed.emit(False)
        try:
            self._log_reply(reply)
        except RuntimeError:
            # reply 可能已经随宿主窗口销毁，日志拿不到就算了。
            pass
        self.handle_reply(
            reply,
            on_success=self._on_success,
            on_error=self._on_error,
        )

    @staticmethod
    def _log_reply(reply: QNetworkReply) -> None:
        """记录状态码与错误枚举：令牌和响应正文都不许落盘。"""
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        logger.info(
            "更新检查响应：HTTP %s，错误枚举 %s",
            status if isinstance(status, int) else None,
            reply.error(),
        )

    def _on_success(self, data: Mapping[str, Any]) -> None:
        try:
            release: ReleaseInfo = parse_release_payload(data)
        except ValueError:
            self.failed.emit(_ERR_BAD_PAYLOAD)
            return
        self.release_ready.emit(release)

    def _on_error(self, outcome: ReplyOutcome) -> None:
        status = outcome.status
        if status is not None and not 200 <= status < 300:
            self.failed.emit(self._message_for_status(status))
            return
        if outcome.network_error or status is None:
            self.failed.emit(_ERR_NETWORK)
            return
        if outcome.malformed:
            self.failed.emit(_ERR_BAD_PAYLOAD)
            return
        self.failed.emit(_ERR_UNEXPECTED)

    @staticmethod
    def _message_for_status(status_code: int) -> str:
        """把 HTTP 状态码映射成中文提示。"""
        if status_code in (403, 429):
            return _ERR_RATE_LIMITED
        if status_code == 404:
            return _ERR_NOT_FOUND
        return f"更新检查失败（HTTP {status_code}）"


__all__ = ["UpdateHTTPClient"]
