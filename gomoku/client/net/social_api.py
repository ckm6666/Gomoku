"""好友、公开资料、私聊消息与头像更新的异步 REST 客户端。

所有成功响应统一发 ``response_ready(kind, data)``；``kind`` 形如
``user:<id>``、``friends``、``messages:<id>``，界面据此分发。

地址拼接、Bearer 头、超时、JSON 解析与 reply 生命周期都由
:class:`~gomoku.client.net.json_api.JsonApiClient` 统一处理，这里只保留
社交相关的 endpoint、信号与响应映射。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkReply

from gomoku.client.config import api_path
from gomoku.client.net.json_api import JsonApiClient, ReplyOutcome


class SocialHTTPClient(JsonApiClient):
    """一个轻量的社交 REST 客户端。

    所有成功响应统一发 ``response_ready(kind, data)``；``kind`` 形如
    ``user:<id>``、``friends``、``messages:<id>``，界面据此分发。
    """

    response_ready = Signal(str, object)
    failed = Signal(str, str)  # (kind, message)
    unauthorized = Signal(str)  # kind
    busy_changed = Signal(bool)

    missing_base_url_message = "未配置联机服务器"
    invalid_url_message = "联机服务器地址无效"
    malformed_payload_message = "服务器响应格式异常"
    default_error_message = "请求失败"

    def __init__(
        self,
        api_base_url: Optional[str],
        timeout_seconds: float = 10.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(api_base_url, timeout_seconds, parent=parent)
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
            api_path("users", quote(user_id, safe="")),
            access_token,
        )

    def update_avatar(self, access_token: str, avatar: str) -> bool:
        return self._start(
            "avatar",
            "PATCH",
            api_path("users", "me", "avatar"),
            access_token,
            {"avatar": avatar},
        )

    def update_avatar_image(
        self, access_token: str, image_data_url: str
    ) -> bool:
        return self._start(
            "avatar",
            "PUT",
            api_path("users", "me", "avatar-image"),
            access_token,
            {"image": image_data_url},
        )

    def delete_avatar_image(self, access_token: str) -> bool:
        return self._start(
            "avatar",
            "DELETE",
            api_path("users", "me", "avatar-image"),
            access_token,
        )

    def search_users(self, access_token: str, query: str) -> bool:
        return self._start(
            "search",
            "GET",
            api_path("users", "search") + f"?q={quote(query, safe='')}",
            access_token,
        )

    def fetch_friends(self, access_token: str) -> bool:
        return self._start(
            "friends", "GET", api_path("friends"), access_token
        )

    def send_friend_request(
        self, access_token: str, user_id: str
    ) -> bool:
        return self._start(
            "friend_request",
            "POST",
            api_path("friends", "requests"),
            access_token,
            {"user_id": user_id},
        )

    def accept_friend_request(
        self, access_token: str, request_id: str
    ) -> bool:
        return self._start(
            "friend_accept",
            "POST",
            api_path(
                "friends", "requests", quote(request_id, safe=""), "accept"
            ),
            access_token,
        )

    def decline_friend_request(
        self, access_token: str, request_id: str
    ) -> bool:
        return self._start(
            "friend_decline",
            "POST",
            api_path(
                "friends", "requests", quote(request_id, safe=""), "decline"
            ),
            access_token,
        )

    def remove_friend(self, access_token: str, user_id: str) -> bool:
        return self._start(
            "friend_remove",
            "DELETE",
            api_path("friends", quote(user_id, safe="")),
            access_token,
        )

    def fetch_messages(
        self, access_token: str, user_id: str, limit: int = 50
    ) -> bool:
        bounded = max(1, min(200, int(limit)))
        return self._start(
            f"messages:{user_id}",
            "GET",
            api_path("messages", quote(user_id, safe=""))
            + f"?limit={bounded}",
            access_token,
        )

    def mark_messages_read(
        self,
        access_token: str,
        user_id: str,
        message_ids: Sequence[str],
    ) -> bool:
        """把已经显示过的消息标记为已读。

        只报真正显示过的 id：服务端据此销账，没看过的消息仍留在未读
        计数里，不会被顺手清零。
        """
        ids = [
            message_id
            for message_id in message_ids
            if isinstance(message_id, str) and message_id
        ][:200]
        if not ids:
            return False
        return self._start(
            f"messages_read:{user_id}",
            "POST",
            api_path("messages", quote(user_id, safe=""), "read"),
            access_token,
            {"message_ids": ids},
        )

    def send_message(
        self, access_token: str, user_id: str, content: str
    ) -> bool:
        return self._start(
            f"message_send:{user_id}",
            "POST",
            api_path("messages", quote(user_id, safe="")),
            access_token,
            {"content": content},
        )

    def abort_all(self) -> None:
        for token in list(self._replies):
            _kind, reply = self._replies.pop(token)
            reply.abort()
            self.discard(reply)
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

        url, error = self.prepare_url(path)
        if url is None:
            self.failed.emit(kind, error)
            return False

        old_token = self._latest_get.get(kind) if method == "GET" else None
        if old_token is not None:
            # 同一个 GET 的上一次请求作废：后发为准，不让过期响应覆盖新状态。
            stale = self._replies.pop(old_token, None)
            if stale is not None:
                stale[1].abort()
                self.discard(stale[1])

        reply = self.send_request(
            method, url, token=access_token, payload=payload
        )
        if reply is None:
            self.failed.emit(kind, self.unsupported_method_message)
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
            self.discard(reply)
            return
        kind, _reply = entry
        if self._latest_get.get(kind) == token:
            self._latest_get.pop(kind, None)
        self.busy_changed.emit(bool(self._replies))

        self.handle_reply(
            reply,
            on_success=lambda data: self.response_ready.emit(
                kind, dict(data)
            ),
            on_error=lambda outcome: self._on_error(kind, outcome),
        )

    def _on_error(self, kind: str, outcome: ReplyOutcome) -> None:
        if outcome.status == 401:
            # 401 交给令牌刷新处理：刷新成功后请求会被重放，这里再报
            # 一遍错只会把“未提供认证凭据”推成红色错误提示。
            self.unauthorized.emit(kind)
            return
        self.failed.emit(
            kind, outcome.error_message or self.default_error_message
        )


__all__ = ["SocialHTTPClient"]
