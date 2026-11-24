"""已认证连接注册表：连接表、IP 配额、好友在线与好友缓存。

``RoomManager`` 把连接相关的状态全部委托给这里；房间/匹配/计时等
领域逻辑不进入本模块。好友 id 缓存使用 LRU + 容量上限，连接建立时
刷新，断开时只读缓存，避免关闭路径再查库。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gomoku.shared.protocol import SERVER_FRIEND_EVENT, make_message
from gomoku.shared.social import (
    FRIEND_EVENT_OFFLINE,
    FRIEND_EVENT_ONLINE,
)
from server.app.config import Settings
from server.app.models import Friendship
from server.app.ws.entities import ClientConnection, Room
from server.app.ws.limits import IpConnectionLimiter
from server.app.ws.payloads import connection_user_payload

logger = logging.getLogger(__name__)

#: 好友 id 缓存的最大条目数（LRU）；超出后淘汰最久未用的。
FRIEND_CACHE_MAX_SIZE = 1024


class ConnectionRegistry:
    """单进程内的连接表与在线状态辅助。"""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        state_lock: asyncio.Lock,
        room_by_user: dict[str, Room],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        # 与 RoomManager 共用同一把全局锁：连接登记与房间归属必须
        # 在同一临界区内保持一致。
        self._state_lock = state_lock
        # 房间归属表的引用（由 RoomManager 维护），用于头像同步。
        self._room_by_user = room_by_user
        self.connections: dict[str, ClientConnection] = {}
        self._connection_limiter = IpConnectionLimiter(
            settings.max_connections_per_ip
        )
        self.friend_cache: "OrderedDict[str, frozenset[str]]" = (
            OrderedDict()
        )

    # ------------------------------------------------------------------
    # 连接/配额
    # ------------------------------------------------------------------
    def is_current_connection(self, conn: ClientConnection) -> bool:
        return self.connections.get(conn.user_id) is conn

    def current_connection(
        self, user_id: str
    ) -> Optional[ClientConnection]:
        """返回该用户当前登记的连接（不判断是否已断开）。"""
        return self.connections.get(user_id)

    def is_online(self, user_id: str) -> bool:
        """用户当前是否有一条活动连接。"""
        conn = self.connections.get(user_id)
        return conn is not None and conn.connected

    def release_locked(self, connection_id: str) -> None:
        """调用方必须持有 ``_state_lock``。"""
        self._connection_limiter.release(connection_id)

    async def reserve_connection(self, conn: ClientConnection) -> bool:
        """认证后登记 IP 配额；同一 connection_id 重复调用是幂等的。"""
        async with self._state_lock:
            existing = self.connections.get(conn.user_id)
            replacing_id = (
                existing.connection_id
                if existing is not None and existing is not conn
                else None
            )
            return self._connection_limiter.reserve(
                conn.client_ip, conn.connection_id, replacing_id
            )

    async def release_connection(self, connection_id: str) -> None:
        async with self._state_lock:
            self.release_locked(connection_id)

    async def update_connection_avatar(
        self, user_id: str, avatar: str, avatar_version: int
    ) -> None:
        """头像在 REST 接口更新后，同步到已认证连接与所在房间。

        WebSocket 连接在认证时缓存了头像；如果用户在大厅里换了头像再
        开局，不刷新缓存会让对手看到旧头像。这里只更新展示字段，不广播，
        下一份对局快照自然带上新头像。
        """
        conn = self.connections.get(user_id)
        safe_version = max(0, int(avatar_version))
        if conn is not None:
            conn.avatar = avatar
            conn.avatar_version = safe_version
        room = self._room_by_user.get(user_id)
        if room is None:
            return
        async with room.lock:
            if room.black_user_id == user_id:
                room.black_avatar = avatar
                room.black_avatar_version = safe_version
            elif room.white_user_id == user_id:
                room.white_avatar = avatar
                room.white_avatar_version = safe_version

    async def send_to_user(self, user_id: str, message: dict) -> bool:
        """向指定用户的当前连接发送一条消息。"""
        conn = self.connections.get(user_id)
        if conn is None or not conn.connected:
            return False
        return await conn.send(message)

    # ------------------------------------------------------------------
    # 好友缓存/在线推送
    # ------------------------------------------------------------------
    def friend_cache_get(self, user_id: str) -> frozenset[str]:
        """读取好友缓存并刷新 LRU 顺序。"""
        cached = self.friend_cache.get(user_id)
        if cached is None:
            return frozenset()
        self.friend_cache.move_to_end(user_id)
        return cached

    def friend_cache_put(
        self, user_id: str, friend_ids: frozenset[str]
    ) -> None:
        """写入好友缓存；超过容量上限就淘汰最久未使用的一条。"""
        self.friend_cache[user_id] = friend_ids
        self.friend_cache.move_to_end(user_id)
        while len(self.friend_cache) > FRIEND_CACHE_MAX_SIZE:
            self.friend_cache.popitem(last=False)

    async def load_friend_ids(self, user_id: str) -> list[str]:
        """查询已接受好友的 id 列表并刷新缓存；数据库异常时返回空表。"""
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.scalars(
                        select(Friendship).where(
                            Friendship.status == "accepted",
                            or_(
                                Friendship.user_a == user_id,
                                Friendship.user_b == user_id,
                            ),
                        )
                    )
                ).all()
        except Exception:
            logger.exception("查询好友列表失败 user=%s", user_id)
            # 数据库抖动时退回缓存快照，不要把好友列表清空——否则这一轮
            # 上下线推送会被静默丢掉，好友的在线状态就此对不上。
            return list(self.friend_cache_get(user_id))
        ids = [
            row.user_b if row.user_a == user_id else row.user_a
            for row in rows
        ]
        self.friend_cache_put(user_id, frozenset(ids))
        return ids

    async def refresh_friend_cache(self, user_id: str) -> None:
        """好友关系变化后刷新缓存（供 REST 接口调用）。"""
        await self.load_friend_ids(user_id)

    async def notify_friends_presence(
        self, conn: ClientConnection, online: bool
    ) -> None:
        """向在线好友推送上下线事件。"""
        if online:
            friend_ids = await self.load_friend_ids(conn.user_id)
        else:
            # 断开时不再查库，使用连接建立时缓存的快照。
            friend_ids = list(self.friend_cache_get(conn.user_id))
        if not friend_ids:
            return
        message = make_message(
            SERVER_FRIEND_EVENT,
            {
                "event": (
                    FRIEND_EVENT_ONLINE
                    if online
                    else FRIEND_EVENT_OFFLINE
                ),
                "user": connection_user_payload(conn),
                "online": online,
            },
        )
        for friend_id in friend_ids:
            target = self.connections.get(friend_id)
            if target is not None:
                await target.send(message)

    def clear(self) -> None:
        """清空连接与配额（服务关闭时使用）。"""
        self.connections.clear()
        self._connection_limiter.clear()


__all__ = ["ConnectionRegistry", "FRIEND_CACHE_MAX_SIZE"]
