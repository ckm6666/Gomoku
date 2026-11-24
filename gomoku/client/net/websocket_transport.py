"""QtWebSocketTransport：把 PySide6 的 QWebSocket 接入统一 Transport 接口。

真实联机使用 wss://；本机开发可以 ws://127.0.0.1。TLS 校验由 Qt 默认执行，
本实现不会调用 ignoreSslErrors()。业务认证在连接后的第一条 JSON 消息中发送，
不把长期 token 放在 URL/查询字符串里。
"""

from __future__ import annotations

import json
import logging
from typing import Mapping, Optional

from PySide6.QtCore import QObject, QUrl
from PySide6.QtNetwork import QAbstractSocket, QNetworkRequest, QSslError
from PySide6.QtWebSockets import QWebSocket

from gomoku.client.transport import Transport
from gomoku.shared.errors import TransportError

logger = logging.getLogger(__name__)


class QtWebSocketTransport(Transport):
    """Qt 事件循环内的 WebSocket 传输。"""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._socket = QWebSocket()
        self._socket.connected.connect(self._on_connected)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.textMessageReceived.connect(self._on_text_message)
        self._socket.errorOccurred.connect(self._on_socket_error)
        self._socket.sslErrors.connect(self._on_ssl_errors)

    def open(
        self,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        parsed = QUrl(url)
        if parsed.scheme() not in ("ws", "wss") or not parsed.host():
            raise TransportError("WebSocket 地址必须是 ws:// 或 wss://")
        if self._socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self._socket.abort()
        self._connected = False

        if headers:
            request = QNetworkRequest(parsed)
            for name, value in headers.items():
                request.setRawHeader(
                    name.encode("ascii", "ignore"),
                    value.encode("utf-8"),
                )
            self._socket.open(request)
        else:
            self._socket.open(parsed)

    def send_message(self, message: dict) -> None:
        if not self._connected:
            raise TransportError("WebSocket 尚未连接")
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        sent = self._socket.sendTextMessage(text)
        if sent < 0:
            raise TransportError("WebSocket 发送失败")

    def close(self) -> None:
        self._connected = False
        try:
            self._socket.close()
        except Exception:
            # 关闭失败也要留痕：否则「连接没断开」这类问题无从排查。
            logger.debug("WebSocket 关闭失败", exc_info=True)

    # ------------------------------------------------------------------
    # QWebSocket 信号
    # ------------------------------------------------------------------
    def _on_connected(self) -> None:
        self._connected = True
        self.connected.emit()

    def _on_disconnected(self) -> None:
        self._connected = False
        reason = self._socket.closeReason() or "WebSocket 连接已断开"
        self.disconnected.emit(reason)

    def _on_text_message(self, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self.error.emit("服务器返回了非法 JSON")
            return
        if not isinstance(message, dict):
            self.error.emit("服务器消息格式非法")
            return
        self.message_received.emit(message)

    def _on_socket_error(self, error: QAbstractSocket.SocketError) -> None:
        if error == QAbstractSocket.SocketError.RemoteHostClosedError:
            return
        message = self._socket.errorString() or "WebSocket 连接错误"
        self.error.emit(message)

    def _on_ssl_errors(self, errors: list[QSslError]) -> None:
        # 不忽略证书错误。Qt 默认会中止连接；这里给 UI 一个明确提示。
        detail = errors[0].errorString() if errors else "TLS 证书校验失败"
        logger.warning("WebSocket TLS 校验失败：%s", detail)
        self.error.emit("TLS 证书校验失败，已拒绝连接")


__all__ = ["QtWebSocketTransport"]
