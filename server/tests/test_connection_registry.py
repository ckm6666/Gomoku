"""ConnectionRegistry 的单元测试：配额、在线状态、缓存与头像同步。"""

from __future__ import annotations

import asyncio
import unittest
from collections import OrderedDict
from types import SimpleNamespace

from gomoku.shared.protocol import SERVER_FRIEND_EVENT
from gomoku.shared.time_control import TimeControl
from server.app.ws.connection_registry import (
    FRIEND_CACHE_MAX_SIZE,
    ConnectionRegistry,
)
from server.app.ws.entities import Room


class _Conn:
    def __init__(
        self,
        user_id: str,
        connection_id: str = "",
        ip: str = "1.1.1.1",
        connected: bool = True,
    ) -> None:
        self.user_id = user_id
        self.connection_id = connection_id or f"cid-{user_id}"
        self.client_ip = ip
        self.connected = connected
        self.username = user_id
        self.avatar = "mei"
        self.avatar_version = 0
        self.rating = 1200
        self.achievements: tuple[str, ...] = ()
        self.sent: list[dict] = []

    async def send(self, message: dict) -> bool:
        if not self.connected:
            return False
        self.sent.append(message)
        return True


def _registry(max_per_ip: int = 200) -> ConnectionRegistry:
    settings = SimpleNamespace(max_connections_per_ip=max_per_ip)
    return ConnectionRegistry(
        settings,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        asyncio.Lock(),
        {},
    )


class CurrentConnectionTests(unittest.TestCase):
    def test_current_connection_and_online(self) -> None:
        registry = _registry()
        conn = _Conn("u1")
        self.assertFalse(registry.is_current_connection(conn))
        self.assertFalse(registry.is_online("u1"))
        registry.connections["u1"] = conn
        self.assertTrue(registry.is_current_connection(conn))
        self.assertIs(registry.current_connection("u1"), conn)
        self.assertTrue(registry.is_online("u1"))
        conn.connected = False
        self.assertFalse(registry.is_online("u1"))


class QuotaTests(unittest.TestCase):
    def test_reserve_is_idempotent_and_replacement_releases_old(self) -> None:
        async def scenario() -> None:
            registry = _registry(max_per_ip=1)
            first = _Conn("u1", "cid-1")
            self.assertTrue(await registry.reserve_connection(first))
            self.assertTrue(await registry.reserve_connection(first))

            registry.connections["u1"] = first
            replacement = _Conn("u1", "cid-2")
            # 同 IP 下替换旧连接净增 0，放得下。
            self.assertTrue(
                await registry.reserve_connection(replacement)
            )
            # 旧连接配额已释放，但新连接仍占满该 IP。
            other = _Conn("u2", "cid-3")
            self.assertFalse(await registry.reserve_connection(other))

            await registry.release_connection("cid-2")
            self.assertTrue(await registry.reserve_connection(other))

        asyncio.run(scenario())

    def test_quota_rejected_keeps_old_connection(self) -> None:
        async def scenario() -> None:
            registry = _registry(max_per_ip=1)
            first = _Conn("u1", "cid-1", ip="2.2.2.2")
            self.assertTrue(await registry.reserve_connection(first))
            # 另一个 IP 已被别的连接占满。
            squatter = _Conn("u9", "cid-9", ip="1.1.1.1")
            self.assertTrue(await registry.reserve_connection(squatter))
            registry.connections["u1"] = first
            # 替换目标在同名用户但不同 IP：净增 1 放不下，必须拒绝。
            outsider = _Conn("u1", "cid-2", ip="1.1.1.1")
            self.assertFalse(await registry.reserve_connection(outsider))
            # 旧连接配额仍保留：同 IP 的新连接仍然放不下。
            self.assertFalse(
                await registry.reserve_connection(
                    _Conn("u8", "cid-8", ip="2.2.2.2")
                )
            )
            self.assertTrue(await registry.reserve_connection(first))

        asyncio.run(scenario())


class SendAndAvatarTests(unittest.TestCase):
    def test_send_to_user(self) -> None:
        async def scenario() -> None:
            registry = _registry()
            conn = _Conn("u1")
            registry.connections["u1"] = conn
            self.assertTrue(
                await registry.send_to_user("u1", {"type": "x"})
            )
            self.assertEqual(conn.sent, [{"type": "x"}])
            self.assertFalse(
                await registry.send_to_user("missing", {"type": "x"})
            )
            conn.connected = False
            self.assertFalse(
                await registry.send_to_user("u1", {"type": "y"})
            )

        asyncio.run(scenario())

    def test_avatar_updates_connection_and_room(self) -> None:
        async def scenario() -> None:
            room_by_user = {}
            settings = SimpleNamespace(max_connections_per_ip=200)
            registry = ConnectionRegistry(
                settings,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                asyncio.Lock(),
                room_by_user,
            )
            conn = _Conn("u1")
            registry.connections["u1"] = conn
            room = Room(
                code="ROOM",
                game_id="game",
                black_user_id="u1",
                white_user_id=None,
                black_name="u1",
                white_name="",
                time_control=TimeControl.per_move(30),
            )
            room_by_user["u1"] = room
            await registry.update_connection_avatar("u1", "he", -1)
            self.assertEqual(conn.avatar, "he")
            self.assertEqual(conn.avatar_version, 0)
            self.assertEqual(room.black_avatar, "he")
            self.assertEqual(room.black_avatar_version, 0)

        asyncio.run(scenario())


class FriendCacheTests(unittest.TestCase):
    def test_lru_is_bounded_and_refreshes_on_get(self) -> None:
        registry = _registry()
        self.assertIsInstance(registry.friend_cache, OrderedDict)
        for index in range(FRIEND_CACHE_MAX_SIZE + 10):
            registry.friend_cache_put(
                str(index), frozenset({str(index)})
            )
        self.assertEqual(
            len(registry.friend_cache), FRIEND_CACHE_MAX_SIZE
        )
        self.assertNotIn("9", registry.friend_cache)
        self.assertIn("10", registry.friend_cache)
        self.assertIn(
            str(FRIEND_CACHE_MAX_SIZE + 9), registry.friend_cache
        )
        self.assertEqual(
            registry.friend_cache_get("10"), frozenset({"10"})
        )
        registry.friend_cache_put("new", frozenset())
        self.assertIn("10", registry.friend_cache)
        self.assertNotIn("11", registry.friend_cache)
        self.assertEqual(registry.friend_cache_get("missing"), frozenset())

    def test_offline_presence_uses_cached_friend_ids(self) -> None:
        async def scenario() -> None:
            registry = _registry()
            user = _Conn("u1")
            friend = _Conn("f1")
            registry.connections["f1"] = friend
            registry.friend_cache_put("u1", frozenset({"f1"}))
            await registry.notify_friends_presence(user, False)
            self.assertEqual(len(friend.sent), 1)
            message = friend.sent[0]
            self.assertEqual(message["type"], SERVER_FRIEND_EVENT)
            self.assertEqual(message["payload"]["user"]["id"], "u1")
            self.assertFalse(message["payload"]["online"])

        asyncio.run(scenario())

    def test_clear(self) -> None:
        registry = _registry()
        registry.connections["u1"] = _Conn("u1")
        registry.clear()
        self.assertEqual(registry.connections, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
