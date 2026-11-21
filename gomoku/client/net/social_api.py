"""好友、公开资料、私聊消息与头像更新的异步 REST 客户端。"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from urllib.parse import quote

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)


class SocialHTTPClient(QObject):
    """一个轻量的社交 REST 客户端。

    所有成功响应统一发 ``response_ready(kind, data)``；``kind`` 形如
    ``user:<id>``、``friends``、``messages:<id>``，界面据此分发。
    """

    response_ready = Signal(str, object)
    failed = Signal(str, str)  # (kind, message)
    unauthorized = Signal(str)  # kind
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
    def fetch_user(self, access_token: str, user_id: str) -> bool:
        return self._start(
            f"user:{user_id}",
            "GET",
            f"/api/v1/users/{quote(user_id, safe='')}",
            access_token,
        )

    def update_avatar(self, access_token: str, avatar: str) -> bool:
        return self._start(
            "avatar",
            "PATCH",
            "/api/v1/users/me/avatar",
            access_token,
            {"avatar": avatar},
        )

    def search_users(self, access_token: str, query: str) -> bool:
        return self._start(
            "search",
            "GET",
            f"/api/v1/users/search?q={quote(query, safe='')}",
            access_token,
        )

    def fetch_friends(self, access_token: str) -> bool:
        return self._start("friends", "GET", "/api/v1/friends", access_token)

    def send_friend_request(
        self, access_token: str, user_id: str
    ) -> bool:
        return self._start(
            "friend_request",
            "POST",
            "/api/v1/friends/requests",
            access_token,
            {"user_id": user_id},
        )

    def accept_friend_request(
        self, access_token: str, request_id: str
    ) -> bool:
        return self._start(
            "friend_accept",
            "POST",
            f"/api/v1/friends/requests/{quote(request_id, safe='')}/accept",
            access_token,
        )

    def decline_friend_request(
        self, access_token: str, request_id: str
    ) -> bool:
        return self._start(
            "friend_decline",
            "POST",
            f"/api/v1/friends/requests/{quote(request_id, safe='')}/decline",
            access_token,
        )

    def remove_friend(self, access_token: str, user_id: str) -> bool:
        return self._start(
            "friend_remove",
            "DELETE",
            f"/api/v1/friends/{quote(user_id, safe='')}",
            access_token,
        )

    def fetch_messages(
        self, access_token: str, user_id: str, limit: int = 50
    ) -> bool:
        bounded = max(1, min(200, int(limit)))
        return self._start(
            f"messages:{user_id}",
            "GET",
            f"/api/v1/messages/{quote(user_id, safe='')}?limit={bounded}",
            access_token,
        )

    def send_message(
        self, access_token: str, user_id: str, content: str
    ) -> bool:
        return self._start(
            f"message_send:{user_id}",
            "POST",
            f"/api/v1/messages/{quote(user_id, safe='')}",
            access_token,
            {"content": content},
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
    def _start(
        self,
        kind: str,
        method: str,
        path: str,
        access_token: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        if not access_token:
            self.failed.emit(kind, "登录状态失效，请重新登录")
            return False
        if not self._api_base_url:
            self.failed.emit(kind, "未配置联机服务器")
            return False

        url = QUrl(f"{self._api_base_url}{path}")
        if url.scheme() not in ("http", "https") or not url.host():
            self.failed.emit(kind, "联机服务器地址无效")
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
        data = QByteArray()
        if payload is not None:
            request.setHeader(
                QNetworkRequest.KnownHeaders.ContentTypeHeader,
                "application/json",
            )
            data = QByteArray(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )

        if method == "GET":
            reply = self._manager.get(request)
        elif method == "POST":
            reply = self._manager.post(request, data)
        elif method == "PATCH":
            reply = self._manager.sendCustomRequest(
                request, b"PATCH", data
            )
        elif method == "DELETE":
            reply = self._manager.deleteResource(request)
        else:
            self.failed.emit(kind, "不支持的请求方法")
            return False

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
            data: Any = {}
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = {}
            success = (
                reply.error() == QNetworkReply.NetworkError.NoError
                or (isinstance(status, int) and 200 <= status < 300)
            )
            if success:
                if not isinstance(data, Mapping):
                    data = {}
                self.response_ready.emit(kind, dict(data))
                return

            message = self._extract_error(data) or "请求失败"
            if status == 401:
                self.unauthorized.emit(kind)
            self.failed.emit(kind, message)
        finally:
            reply.deleteLater()

    @staticmethod
    def _extract_error(data: Any) -> str:
        if isinstance(data, Mapping):
            detail = data.get("detail")
            if isinstance(detail, str):
                return detail
            if isinstance(detail, list):
                parts = [
                    item.get("msg", "")
                    for item in detail
                    if isinstance(item, Mapping)
                ]
                return "；".join(part for part in parts if part)
        return ""


__all__ = ["SocialHTTPClient"]
