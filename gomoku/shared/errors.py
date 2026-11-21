"""客户端与服务端共用的基础错误类型。"""

from __future__ import annotations


class GomokuError(Exception):
    """项目自定义错误的基类。"""


class ConfigurationError(GomokuError):
    """本地配置/服务器地址配置非法。"""


class ProtocolError(GomokuError):
    """网络消息格式、版本或字段不合法。"""


class TransportError(GomokuError):
    """传输层连接、发送或接收失败。"""


class SessionError(GomokuError):
    """会话状态不允许当前操作。"""


__all__ = [
    "GomokuError",
    "ConfigurationError",
    "ProtocolError",
    "TransportError",
    "SessionError",
]
