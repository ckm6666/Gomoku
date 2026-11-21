"""测试用可注入传输，不发起真实网络连接。"""

from __future__ import annotations

import copy
from typing import Mapping, Optional

from gomoku.client.transport import Transport


class FakeTransport(Transport):
    """记录发送消息并允许测试手动注入服务器事件的 Transport。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.sent_messages: list[dict] = []
        self.opened_urls: list[str] = []
        self.close_calls = 0
        self.raise_on_send: Optional[Exception] = None

    def open(
        self,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.opened_urls.append(url)
        self._connected = True
        self.connected.emit()

    def send_message(self, message: dict) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent_messages.append(copy.deepcopy(message))

    def close(self) -> None:
        self.close_calls += 1
        was_connected = self._connected
        self._connected = False
        if was_connected:
            self.disconnected.emit("closed by client")

    # 测试便捷方法 -----------------------------------------------------
    def receive(self, message: Mapping) -> None:
        """模拟服务器发来一条消息。"""
        self.message_received.emit(dict(message))

    def fail(self, message: str) -> None:
        """模拟传输层错误。"""
        self.error.emit(message)

    def drop(self, reason: str = "connection lost") -> None:
        """模拟连接中断。"""
        self._connected = False
        self.disconnected.emit(reason)
