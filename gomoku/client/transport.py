"""客户端网络传输接口。

Stage 0 只定义与 UI 事件循环兼容的抽象和测试用 fake transport；真正的
QtWebSocket/WSS 实现将在联机阶段加入 PySide6-Addons 后提供。把传输抽象
在这里可以让 OnlineSession 使用假传输做确定性的状态机测试，而不依赖网络。
"""

from __future__ import annotations

from typing import Mapping, Optional

from PySide6.QtCore import QObject, Signal


class Transport(QObject):
    """异步传输接口（HTTPS REST 不在此类，WebSocket 对局通道由此承载）。

    具体实现负责：
    * 建立/关闭连接与 TLS 校验；
    * 将 JSON 兼容的 dict 编码为协议帧；
    * 将网络线程/事件循环的结果发到本 QObject 所在（GUI）线程。
    """

    connected = Signal()
    disconnected = Signal(str)
    message_received = Signal(object)
    error = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def open(
        self,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """建立连接；真实实现可能异步完成并发出 connected 信号。"""
        raise NotImplementedError

    def send_message(self, message: dict) -> None:
        """发送一条已编码为 JSON dict 的协议消息。"""
        raise NotImplementedError

    def close(self) -> None:
        """关闭连接；多次调用应安全。"""
        raise NotImplementedError


__all__ = ["Transport"]
