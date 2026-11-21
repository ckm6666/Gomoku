"""排位资料与排行榜的异步 HTTP 客户端。

与 :class:`AuthHTTPClient` 分开使用独立的 QNetworkAccessManager，避免资料
刷新与登录令牌刷新互相抢占“单请求”状态。两个 GET 可以并行；同类请求会
取消旧请求后重新发起。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

logger = logging.getLogger(__name__)

_PROFILE_PATH = "/api/v1/profile/me"


class ProfileHTTPClient(QObject):
    """读取当前用户排位资料与排行榜。"""

    profile_ready = Signal(object)  # dict: {"user": {...}, "recent_games": [...]}
    leaderboard_ready = Signal(object)  # dict: {"entries": [...], "total": n}
    failed = Signal(str)
    unauthorized = Signal()
    busy_changed = Signal(bool)

    def __init__(
        self,
        api_base_url: Optional[str],
        timeout_seconds: float = 10.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._api_base_url = (api_base_url or "").rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._manager = QNetworkAccessManager(self)
        self._replies: dict[str, QNetworkReply] = {}

    @property
    def busy(self) -> bool:
        return bool(self._replies)

    # ------------------------------------------------------------------
    # 公开请求
    # ------------------------------------------------------------------
    def fetch_profile(self, access_token: str) -> bool:
        return self._start("profile", _PROFILE_PATH, access_token)

    def fetch_leaderboard(
        self, access_token: str, limit: int = 50
    ) -> bool:
        bounded = max(1, min(100, int(limit)))
        return self._start(
            "leaderboard",
            f"/api/v1/leaderboard?limit={bounded}",
            access_token,
        )

    def abort_all(self) -> None:
        for kind in list(self._replies):
            reply = self._replies.pop(kind, None)
            if reply is not None:
                reply.abort()
                reply.deleteLater()
        self.busy_changed.emit(False)

    # ------------------------------------------------------------------
    # 请求/响应
    # ------------------------------------------------------------------
    def _start(self, kind: str, path: str, access_token: str) -> bool:
        if not access_token:
            self.failed.emit("登录状态失效，请重新登录")
            return False
        if not self._api_base_url:
            self.failed.emit("未配置联机服务器")
            return False

        url = QUrl(f"{self._api_base_url}{path}")
        if url.scheme() not in ("http", "https") or not url.host():
            self.failed.emit("联机服务器地址无效")
            return False

        old_reply = self._replies.pop(kind, None)
        if old_reply is not None:
            old_reply.abort()
            old_reply.deleteLater()

        request = QNetworkRequest(url)
        request.setRawHeader(
            QByteArray(b"Authorization"),
            QByteArray(f"Bearer {access_token}".encode("utf-8")),
        )
        request.setTransferTimeout(int(self._timeout_seconds * 1000))

        reply = self._manager.get(request)
        self._replies[kind] = reply
        reply.finished.connect(
            lambda reply=reply, kind=kind: self._on_finished(reply, kind)
        )
        self.busy_changed.emit(True)
        return True

    def _on_finished(self, reply: QNetworkReply, kind: str) -> None:
        if reply is not self._replies.get(kind):
            reply.deleteLater()
            return

        self._replies.pop(kind, None)
        self.busy_changed.emit(bool(self._replies))

        try:
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            raw = bytes(reply.readAll())
            data: Any = None
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None

            if (
                reply.error() == QNetworkReply.NetworkError.NoError
                or (isinstance(status, int) and 200 <= status < 300)
            ):
                if not isinstance(data, Mapping):
                    self.failed.emit("服务器返回的排位数据格式异常")
                    return
                if kind == "profile":
                    self.profile_ready.emit(dict(data))
                else:
                    self.leaderboard_ready.emit(dict(data))
                return

            if status == 401:
                self.unauthorized.emit()
            self.failed.emit(self._extract_error(data) or "读取排位数据失败")
        finally:
            reply.deleteLater()

    @staticmethod
    def _extract_error(data: Any) -> str:
        if not isinstance(data, Mapping):
            return ""
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail[:300]
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, Mapping):
                message = first.get("msg")
                if isinstance(message, str):
                    return message[:300]
        return ""


__all__ = ["ProfileHTTPClient"]
