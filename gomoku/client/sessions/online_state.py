"""联机会话的高层阶段枚举。

连接是否断开是正交状态，用 ``OnlineGameSession.is_connected`` 表示；
``phase`` 只描述用户当前处在哪个界面阶段，避免 UI 层同时读取多个布尔量。
"""

from __future__ import annotations

from enum import Enum


class OnlineSessionPhase(Enum):
    CLOSED = "closed"          # 会话已关闭
    CONNECTING = "connecting"  # 尚未认证
    LOBBY = "lobby"            # 已认证，不在队列/房间中
    QUEUED = "queued"          # 排位匹配队列
    WAITING = "waiting"        # 自建房等待对手
    PLAYING = "playing"        # 玩家正在对局
    SPECTATING = "spectating"  # 旁观进行中的对局


__all__ = ["OnlineSessionPhase"]
