"""基于 QtNetwork 的异步账号 HTTP 客户端。

QtNetwork 已包含在 PySide6-Essentials 中；请求在 Qt 事件循环里异步完成，
不阻塞 GUI。这里不忽略 TLS 错误，也不把密码/token 写入日志。

地址拼接、Bearer 头、超时、JSON 解析与 reply 生命周期都由
:class:`~gomoku.client.net.json_api.JsonApiClient` 统一处理，这里只保留
登录相关的 endpoint、信号与响应映射。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkReply

from gomoku.client.auth import AuthSession
from gomoku.client.config import api_path
from gomoku.client.net.json_api import JsonApiClient, ReplyOutcome
from gomoku.shared.errors import ProtocolError

logger = logging.getLogger(__name__)


class AuthHTTPClient(JsonApiClient):
    """登录、注册、刷新与登出；一次只允许一个在途请求。"""

    session_ready = Signal(object)  # AuthSession（登录/注册成功）
    session_refreshed = Signal(object)  # AuthSession
    logged_out = Signal()
    failed = Signal(str)
    busy_changed = Signal(bool)

    missing_base_url_message = "未配置登录服务器"
    invalid_url_message = "登录服务器地址无效"
    malformed_payload_message = "服务器响应格式异常"
    default_error_message = "网络请求失败，请检查服务器是否已启动"

    def __init__(
        self,
        api_base_url: Optional[str],
        timeout_seconds: float = 10.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(api_base_url, timeout_seconds, parent=parent)
        self._active_reply: Optional[QNetworkReply] = None
        self._active_kind = ""
        self._session: Optional[AuthSession] = None
        self._last_error_status: Optional[int] = None

    @property
    def session(self) -> Optional[AuthSession]:
        return self._session

    @property
    def busy(self) -> bool:
        return self._active_reply is not None

    @property
    def last_error_status(self) -> Optional[int]:
        """最近一次失败请求的 HTTP 状态码；网络错误时为 None。"""
        return self._last_error_status

    # ------------------------------------------------------------------
    # 公开请求
    # ------------------------------------------------------------------
    def login(self, username: str, password: str) -> bool:
        return self._start(
            "login",
            "login",
            {"username": username, "password": password},
        )

    def register(self, username: str, password: str) -> bool:
        return self._start(
            "register",
            "register",
            {
                "username": username,
                "password": password,
            },
        )

    def refresh(self, refresh_token: Optional[str] = None) -> bool:
        token = refresh_token or (
            self._session.refresh_token if self._session else ""
        )
        if not token:
            self.failed.emit("登录已失效，请重新登录")
            return False
        return self._start("refresh", "refresh", {"refresh_token": token})

    def logout(self) -> bool:
        session = self._session
        if session is None:
            self.logged_out.emit()
            return True
        return self._start(
            "logout",
            "logout",
            {"refresh_token": session.refresh_token},
            access_token=session.access_token,
        )

    def abort(self) -> None:
        reply = self._active_reply
        if reply is not None:
            # 先摘掉引用再 abort：真实 Qt 会在 abort 后发出 finished，
            # 那时这条响应已经过期，不能再触发任何结果信号。
            self._active_reply = None
            self._active_kind = ""
            try:
                reply.abort()
            except RuntimeError:
                pass
            self.discard(reply)
            self.busy_changed.emit(False)

    # ------------------------------------------------------------------
    # 请求/响应
    # ------------------------------------------------------------------
    def _start(
        self,
        kind: str,
        path: str,
        payload: Mapping[str, Any],
        access_token: Optional[str] = None,
    ) -> bool:
        if self.busy:
            self.failed.emit("请求处理中，请稍候")
            return False
        if not self.base_url:
            # 没有配置地址时保留上一次的状态码：界面据此判断要不要刷新令牌。
            self.failed.emit(self.missing_base_url_message)
            return False

        self._last_error_status = None
        url, error = self.prepare_url(api_path("auth", path))
        if url is None:
            self.failed.emit(error)
            return False

        reply = self.send_request(
            "POST",
            url,
            token=access_token or "",
            payload=payload,
        )
        if reply is None:
            self.failed.emit(self.unsupported_method_message)
            return False

        self._active_reply = reply
        self._active_kind = kind
        reply.finished.connect(
            lambda reply=reply, kind=kind: self._on_finished(reply, kind)
        )
        self.busy_changed.emit(True)
        return True

    def _on_finished(self, reply: QNetworkReply, kind: str) -> None:
        if reply is not self._active_reply:
            self.discard(reply)
            return

        self._active_reply = None
        self._active_kind = ""
        self.busy_changed.emit(False)

        self.handle_reply(
            reply,
            on_success=lambda data: self._handle_success(kind, data),
            on_error=self._on_error,
        )

    def _on_error(self, outcome: ReplyOutcome) -> None:
        # 只有真正的 HTTP 错误才算状态码：2xx 但正文损坏不算“服务器拒绝”。
        if outcome.status is not None and 200 <= outcome.status < 300:
            self._last_error_status = None
        else:
            self._last_error_status = outcome.status
        message = outcome.error_message or self.fallback_message(outcome)
        self.failed.emit(message)

    def fallback_message(self, outcome: ReplyOutcome) -> str:
        """没有错误详情时的兜底文案；429 单独提示。"""
        if outcome.status == 429:
            return "操作过于频繁，请稍后再试"
        return self.default_error_message

    def _handle_success(self, kind: str, data: Mapping[str, Any]) -> None:
        if kind == "logout":
            self._session = None
            self.logged_out.emit()
            return

        try:
            session = AuthSession.from_response(data)
        except ProtocolError as exc:
            self.failed.emit(str(exc))
            return

        self._session = session
        if kind == "refresh":
            self.session_refreshed.emit(session)
        else:
            self.session_ready.emit(session)


__all__ = ["AuthHTTPClient"]
