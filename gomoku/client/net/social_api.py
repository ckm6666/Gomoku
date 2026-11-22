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
        # 每个在途请求都有自己的令牌：同一 kind 的多个写请求（例如连续
        # 发两条私聊）可以同时在飞，不会互相顶掉。
        self._replies: dict[int, tuple[str, QNetworkReply]] = {}
        # GET 走“后发先到为准”：同一 kind 的新请求会作废上一个。
        self._latest_get: dict[str, int] = {}
        self._next_token = 0

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

    def update_avatar_image(
        self, access_token: str, image_data_url: str
    ) -> bool:
        return self._start(
            "avatar",
            "PUT",
            "/api/v1/users/me/avatar-image",
            access_token,
            {"image": image_data_url},
        )

    def delete_avatar_image(self, access_token: str) -> bool:
        return self._start(
            "avatar",
            "DELETE",
            "/api/v1/users/me/avatar-image",
            access_token,
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
        for token in list(self._replies):
            _kind, reply = self._replies.pop(token)
            reply.abort()
            reply.deleteLater()
        self._latest_get.clear()
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

        old_token = self._latest_get.get(kind) if method == "GET" else None
        if old_token is not None:
            # 同一个 GET 的上一次请求作废：后发为准，不让过期响应覆盖新状态。
            stale = self._replies.pop(old_token, None)
            if stale is not None:
                stale[1].abort()
                stale[1].deleteLater()

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
        elif method in ("PATCH", "PUT"):
            reply = self._manager.sendCustomRequest(
                request, method.encode("ascii"), data
            )
        elif method == "DELETE":
            reply = self._manager.deleteResource(request)
        else:
            self.failed.emit(kind, "不支持的请求方法")
            return False

        self._next_token += 1
        token = self._next_token
        self._replies[token] = (kind, reply)
        if method == "GET":
            self._latest_get[kind] = token
        reply.finished.connect(
            lambda reply=reply, token=token: self._on_finished(reply, token)
        )
        self.busy_changed.emit(True)
        return True

    def _on_finished(self, reply: QNetworkReply, token: int) -> None:
        entry = self._replies.pop(token, None)
        if entry is None:
            # 已经被作废（例如同 kind 的新 GET 顶掉了它）：直接丢弃。
            reply.deleteLater()
            return
        kind, _reply = entry
        if self._latest_get.get(kind) == token:
            self._latest_get.pop(kind, None)
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
                # 401 交给令牌刷新处理：刷新成功后请求会被重放，这里再报
                # 一遍错只会把“未提供认证凭据”推成红色错误提示。
                self.unauthorized.emit(kind)
                return
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
