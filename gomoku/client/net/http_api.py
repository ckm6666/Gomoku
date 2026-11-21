"""基于 QtNetwork 的异步账号 HTTP 客户端。

QtNetwork 已包含在 PySide6-Essentials 中；请求在 Qt 事件循环里异步完成，
不阻塞 GUI。这里不忽略 TLS 错误，也不把密码/token 写入日志。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from gomoku.client.auth import AuthSession
from gomoku.shared.errors import ProtocolError

logger = logging.getLogger(__name__)


class AuthHTTPClient(QObject):
    """登录、注册、刷新与登出；一次只允许一个在途请求。"""

    session_ready = Signal(object)  # AuthSession（登录/注册成功）
    session_refreshed = Signal(object)  # AuthSession
    logged_out = Signal()
    failed = Signal(str)
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
        self._active_reply: Optional[QNetworkReply] = None
        self._active_kind = ""
        self._session: Optional[AuthSession] = None

    @property
    def session(self) -> Optional[AuthSession]:
        return self._session

    @property
    def busy(self) -> bool:
        return self._active_reply is not None

    # ------------------------------------------------------------------
    # 公开请求
    # ------------------------------------------------------------------
    def login(self, username: str, password: str) -> bool:
        return self._start(
            "login",
            "login",
            {"username": username, "password": password},
        )

    def register(self, username: str, password: str, invite_code: str) -> bool:
        return self._start(
            "register",
            "register",
            {
                "username": username,
                "password": password,
                "invite_code": invite_code,
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
            reply.abort()
            reply.deleteLater()
            self._active_reply = None
            self._active_kind = ""
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
        if not self._api_base_url:
            self.failed.emit("未配置登录服务器")
            return False

        url = QUrl(f"{self._api_base_url}/api/v1/auth/{path}")
        if url.scheme() not in ("http", "https") or not url.host():
            self.failed.emit("登录服务器地址无效")
            return False

        request = QNetworkRequest(url)
        request.setHeader(
            QNetworkRequest.KnownHeaders.ContentTypeHeader,
            "application/json",
        )
        request.setTransferTimeout(int(self._timeout_seconds * 1000))
        if access_token:
            request.setRawHeader(
                QByteArray(b"Authorization"),
                QByteArray(f"Bearer {access_token}".encode("utf-8")),
            )

        body = QByteArray(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        reply = self._manager.post(request, body)
        self._active_reply = reply
        self._active_kind = kind
        reply.finished.connect(
            lambda reply=reply, kind=kind: self._on_finished(reply, kind)
        )
        self.busy_changed.emit(True)
        return True

    def _on_finished(self, reply: QNetworkReply, kind: str) -> None:
        if reply is not self._active_reply:
            reply.deleteLater()
            return

        self._active_reply = None
        self._active_kind = ""
        self.busy_changed.emit(False)

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
                self._handle_success(kind, data, status)
                return

            message = self._extract_error(data)
            if not message:
                if status == 429:
                    message = "操作过于频繁，请稍后再试"
                else:
                    message = "网络请求失败，请检查服务器是否已启动"
            self.failed.emit(message)
        finally:
            reply.deleteLater()

    def _handle_success(
        self, kind: str, data: Any, status: Optional[int]
    ) -> None:
        if kind == "logout":
            self._session = None
            self.logged_out.emit()
            return

        if not isinstance(data, Mapping):
            self.failed.emit("服务器响应格式异常")
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


__all__ = ["AuthHTTPClient"]
