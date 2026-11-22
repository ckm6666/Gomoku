"""联机协议能力声明。

能力列表只用于“功能是否可用”的协商，不替代权限校验：即使客户端声明
支持某项能力，服务端仍必须独立校验。未知能力应被忽略，以便后续版本
在同一协议版本内追加功能。
"""

from __future__ import annotations

from typing import Any, Sequence

from gomoku.shared.errors import ProtocolError

CAP_SPECTATE = "spectate"
CAP_LIVE_GAMES = "live_games"
CAP_UNDO_REQUEST = "undo_request"
CAP_UNDO_QUOTA = "undo_quota"
CAP_ROOM_OPTIONS = "room_options"

MAX_CAPABILITIES = 32
MAX_CAPABILITY_LENGTH = 64

#: 当前服务端支持的能力。
SERVER_CAPABILITIES = frozenset(
    {
        CAP_SPECTATE,
        CAP_LIVE_GAMES,
        CAP_UNDO_REQUEST,
        CAP_UNDO_QUOTA,
        CAP_ROOM_OPTIONS,
    }
)

#: 当前客户端理解的服务端能力。
CLIENT_CAPABILITIES = frozenset(SERVER_CAPABILITIES)


def normalize_capabilities(value: Any) -> tuple[str, ...]:
    """校验并返回去重后的能力名；畸形输入抛 ``ProtocolError``。"""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProtocolError("capabilities 必须是字符串数组")
    if len(value) > MAX_CAPABILITIES:
        raise ProtocolError("capabilities 数量过多")

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ProtocolError("capabilities 只能包含字符串")
        name = item.strip()
        if not name or len(name) > MAX_CAPABILITY_LENGTH:
            raise ProtocolError("capability 名称非法")
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


__all__ = [
    "CAP_LIVE_GAMES",
    "CAP_ROOM_OPTIONS",
    "CAP_SPECTATE",
    "CAP_UNDO_QUOTA",
    "CAP_UNDO_REQUEST",
    "CLIENT_CAPABILITIES",
    "MAX_CAPABILITIES",
    "MAX_CAPABILITY_LENGTH",
    "SERVER_CAPABILITIES",
    "normalize_capabilities",
]
