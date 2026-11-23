"""更新检查：从 GitHub Releases API 异步获取版本元数据。

版本信息只来自 GitHub Releases API：客户端自身**不提供**安装包下载入口，
安装包仍由用户从发布页手动获取，因此这里只负责读取 JSON 元数据并交给
界面层判断版本。

两层分工：
- 本模块（网络层）只发请求、解码 JSON、映射错误码；
- 界面层负责比较版本号并在浏览器里打开发布页。

日志里只允许出现状态码与错误枚举：令牌和响应正文都不许落盘。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from gomoku.client.constants import APP_VERSION, GITHUB_RELEASE_API_URL
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


class UpdateHTTPClient(QObject):
    """向 GitHub Releases API 查询最新版本；同一时刻只允许一个在途请求。"""

    release_ready = Signal(object)  # ReleaseInfo
    failed = Signal(str)  # 中文错误消息
    busy_changed = Signal(bool)

    def __init__(
        self,
        api_url: str = GITHUB_RELEASE_API_URL,
        timeout_seconds: float = 10.0,
        token: str = "",
        *,
        manager: Optional[QNetworkAccessManager] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._api_url = api_url or ""
        self._timeout_seconds = timeout_seconds
        # 令牌只用于提高 GitHub API 的频率上限；绝不入日志。
        self._token = token or ""
        # manager 可注入，便于测试时替换成假的对象。
        self._manager = (
            manager if manager is not None else QNetworkAccessManager(self)
        )
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

        url_text = self._api_url.strip()
        if not url_text:
            self.failed.emit(_ERR_BAD_URL)
            return False
        url = QUrl(url_text)
        if url.scheme() not in ("http", "https") or not url.host():
            self.failed.emit(_ERR_BAD_URL)
            return False

        request = self._build_request(url)
        try:
            reply = self._manager.get(request)
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
    def _build_request(self, url: QUrl) -> QNetworkRequest:
        """构造带 GitHub 约定请求头的 GET 请求。"""
        request = QNetworkRequest(url)
        request.setRawHeader(
            QByteArray(b"Accept"), QByteArray(b"application/vnd.github+json")
        )
        request.setRawHeader(
            QByteArray(b"X-GitHub-Api-Version"), QByteArray(b"2022-11-28")
        )
        request.setRawHeader(
            QByteArray(b"User-Agent"),
            QByteArray(f"Gomoku/{APP_VERSION}".encode("utf-8")),
        )
        # 令牌只发给 GitHub 官方 API：api_url 是可配置的，万一被改成第三方
        # 地址，把个人访问令牌带过去等于把凭据交出去。
        if self._token and url.host().lower() == "api.github.com":
            request.setRawHeader(
                QByteArray(b"Authorization"),
                QByteArray(f"Bearer {self._token}".encode("utf-8")),
            )
        request.setTransferTimeout(int(self._timeout_seconds * 1000))
        return request

    def _on_finished(self, reply: QNetworkReply) -> None:
        """处理一次请求的结束。"""
        if reply is not self._reply:
            # 已经被 abort() 取消的旧响应：静默丢弃，不发任何信号。
            self._discard(reply)
            return

        self._reply = None
        # 先复位忙状态，界面就不会在提示框弹出时还显示“检查中”。
        self.busy_changed.emit(False)
        try:
            self._handle(reply)
        except RuntimeError:
            # 宿主窗口已经销毁，reply 也跟着没了。槽函数里抛出的异常
            # 会让 PySide 直接中止整个进程，这里必须静默收场。
            return
        except Exception:  # pragma: no cover - 兜底，界面不能被异常打断
            logger.exception("更新检查处理异常")
            self.failed.emit(_ERR_UNEXPECTED)
        finally:
            self._discard(reply)

    def _handle(self, reply: QNetworkReply) -> None:
        """按状态码与错误枚举分流，产出结果信号。"""
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status_code = status if isinstance(status, int) else None
        error = reply.error()
        logger.info("更新检查响应：HTTP %s，错误枚举 %s", status_code, error)

        if status_code is not None and not 200 <= status_code < 300:
            self.failed.emit(self._message_for_status(status_code))
            return
        if error != QNetworkReply.NetworkError.NoError or status_code is None:
            # 没有 HTTP 状态码却带错误枚举：典型的连不上/超时。
            self.failed.emit(_ERR_NETWORK)
            return

        payload = self._decode_payload(reply)
        if payload is None:
            self.failed.emit(_ERR_BAD_PAYLOAD)
            return

        try:
            release: ReleaseInfo = parse_release_payload(payload)
        except ValueError:
            self.failed.emit(_ERR_BAD_PAYLOAD)
            return
        self.release_ready.emit(release)

    @staticmethod
    def _decode_payload(reply: QNetworkReply) -> Optional[Mapping[str, Any]]:
        """把响应正文解码成 JSON 对象；格式不对时返回 ``None``。"""
        # QByteArray.data() 已经是字节串，这里再包一层 bytes 是为了
        # 兼容不同 PySide6 版本返回的 bytes / bytearray / memoryview。
        raw = bytes(reply.readAll().data())
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        return payload

    @staticmethod
    def _message_for_status(status_code: int) -> str:
        """把 HTTP 状态码映射成中文提示。"""
        if status_code in (403, 429):
            return _ERR_RATE_LIMITED
        if status_code == 404:
            return _ERR_NOT_FOUND
        return f"更新检查失败（HTTP {status_code}）"

    @staticmethod
    def _discard(reply: QNetworkReply) -> None:
        """安全地销毁 reply：它可能已经随宿主窗口一起没了。"""
        try:
            reply.deleteLater()
        except RuntimeError:
            pass


__all__ = ["UpdateHTTPClient"]
