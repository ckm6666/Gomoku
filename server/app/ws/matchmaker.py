"""快速匹配队列：入队、清理、选对手与失败回填。

队列是单进程内存状态，所有读写都必须在 ``RoomManager`` 的全局
``_state_lock`` 内完成。``Matchmaker`` 只操作队列本身；连接表与房间
归属通过构造时注入的注册表/字典读取，避免反向依赖门面。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Optional

from gomoku.shared.time_control import TimeControl
from server.app.ws.connection_registry import ConnectionRegistry
from server.app.ws.entities import (
    ClientConnection,
    QueuedPlayer,
    Room,
)
from server.app.ws.payloads import queue_state_payload


class Matchmaker:
    """排位匹配队列的状态与选择逻辑。"""

    def __init__(
        self,
        state_lock: asyncio.Lock,
        connections: ConnectionRegistry,
        room_by_user: dict[str, Room],
    ) -> None:
        self._state_lock = state_lock
        self._connections = connections
        self._room_by_user = room_by_user
        self.queue: dict[str, QueuedPlayer] = {}

    def __len__(self) -> int:
        return len(self.queue)

    def get(self, user_id: str) -> Optional[QueuedPlayer]:
        return self.queue.get(user_id)

    def put(self, player: QueuedPlayer) -> None:
        self.queue[player.conn.user_id] = player

    def remove(self, user_id: str) -> None:
        self.queue.pop(user_id, None)

    def rebind_connection(self, conn: ClientConnection) -> None:
        """重连后把队列项指向新连接，不改变排队时间/积分/时限。"""
        queued = self.queue.get(conn.user_id)
        if queued is not None:
            queued.conn = conn

    def refresh(
        self, conn: ClientConnection, time_control: TimeControl
    ) -> None:
        """重复入队时更新连接与时限，不改变原有排队时间/积分。"""
        queued = self.queue.get(conn.user_id)
        if queued is not None:
            queued.conn = conn
            queued.time_control = time_control

    def state_payload(self, user_id: str) -> dict:
        return queue_state_payload(user_id, self.queue)

    def prune_locked(self) -> None:
        """清理已断线或已被新连接替换的队列项。"""
        for user_id, queued in list(self.queue.items()):
            if (
                not queued.conn.connected
                or self._connections.current_connection(user_id)
                is not queued.conn
            ):
                self.queue.pop(user_id, None)

    def pop_match_locked(
        self, conn: ClientConnection
    ) -> Optional[tuple[QueuedPlayer, QueuedPlayer]]:
        """锁内选对手并弹出双方队列项；不足两人时返回 None。

        弹出即视为预占：数据库写库在锁外进行，这段时间其他匹配命令
        看不到这两个玩家，避免同一人被两个对局同时领走。调用方必须
        持有 ``_state_lock``。
        """
        queued = self.queue.get(conn.user_id)
        if queued is None:
            return None
        candidates = [
            player
            for user_id, player in self.queue.items()
            if user_id != conn.user_id
        ]
        if not candidates:
            return None
        opponent = min(
            candidates, key=lambda player: abs(player.rating - queued.rating)
        )
        if secrets.randbelow(2) == 0:
            black, white = queued, opponent
        else:
            black, white = opponent, queued
        self.queue.pop(black.conn.user_id, None)
        self.queue.pop(white.conn.user_id, None)
        return black, white

    async def requeue_opponent_after_failure(
        self,
        conn: ClientConnection,
        pair: tuple[QueuedPlayer, QueuedPlayer],
    ) -> None:
        """配对失败后把对手放回队列，保留"发起者退出、等待者继续"语义。"""
        other = pair[1] if pair[0].conn.user_id == conn.user_id else pair[0]
        async with self._state_lock:
            if (
                self._connections.current_connection(
                    other.conn.user_id
                ) is other.conn
                and other.conn.user_id not in self._room_by_user
                and other.conn.user_id not in self.queue
            ):
                other.queued_at = time.monotonic()
                self.queue[other.conn.user_id] = other

    def clear(self) -> None:
        self.queue.clear()


__all__ = ["Matchmaker"]
