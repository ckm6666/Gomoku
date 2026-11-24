"""联机一致性与并发边界的回归测试。

覆盖审查报告中的服务端条目：落子幂等重放的优先级、终局与积分
结算的事务边界、悔棋写库失败的收尾、超时原因共用、连接身份校验、
全局锁内不得访问数据库、广播并发化、邀请/好友缓存上限、连接配额
替换以及错误响应回显 request_id。
"""

from __future__ import annotations

import asyncio
import time
import unittest
from collections import OrderedDict
from types import SimpleNamespace

from sqlalchemy import func, select

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_QUICK_MATCH,
    CLIENT_RESIGN,
    CLIENT_UNDO_REQUEST,
    CLIENT_UNDO_RESPONSE,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_QUEUE_STATUS,
    SERVER_ROOM_CREATED,
    SERVER_UNDO_STATUS,
    make_message,
    parse_game_state,
)
from gomoku.shared.rules import Player
from gomoku.shared.time_control import DEFAULT_TIME_CONTROL
from server.app.db import create_engine, create_session_factory
from server.app.metrics import ServerMetrics
from server.app.models import Game, Move, RatingEvent, User, new_id
from server.app.ws import rooms as rooms_module
from server.app.ws.connection_registry import FRIEND_CACHE_MAX_SIZE
from server.app.ws.entities import Room, RoomInvite
from server.app.ws.limits import IpConnectionLimiter
from server.app.ws.rooms import RoomManager
from server.tests.helpers import ServerTestBase


def _bare_manager() -> RoomManager:
    """只需要内存领域服务的 RoomManager；不接触数据库。"""
    settings = SimpleNamespace(max_connections_per_ip=200)
    return RoomManager(settings, None, ServerMetrics())  # type: ignore[arg-type]


class _WsTestBase(ServerTestBase):
    def _register_access(self, username: str) -> tuple[str, str]:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        return data["access_token"], data["user"]["id"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def _receive_type(self, ws, expected: str, attempts: int = 30) -> dict:
        for _ in range(attempts):
            message = ws.receive_json()
            if message["type"] == expected:
                return message
        self.fail(f"未收到期望消息 {expected}")
        return {}

    def _open_friend_game(self, host, guest) -> str:
        host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
        created = host.receive_json()
        self.assertEqual(created["type"], SERVER_ROOM_CREATED)
        code = created["payload"]["room_code"]
        guest.send_json(
            make_message(CLIENT_JOIN_ROOM, {"room_code": code})
        )
        host_state = host.receive_json()
        guest_state = guest.receive_json()
        self.assertEqual(host_state["type"], SERVER_GAME_STATE)
        self.assertEqual(guest_state["type"], SERVER_GAME_STATE)
        return host_state["payload"]["game_id"]

    def _move(self, sender, other, row: int, col: int, request_id: str):
        sender.send_json(
            make_message(
                CLIENT_MOVE_INTENT,
                {"row": row, "col": col},
                request_id=request_id,
            )
        )
        sent = sender.receive_json()
        other.receive_json()
        return sent


class MoveReplayOrderTests(_WsTestBase):
    """S1：重放请求必须早于回合/坐标/占用/终局校验。"""

    def test_replay_wins_over_turn_occupancy_and_game_over(self) -> None:
        host_token, _ = self._register_access("replay_host")
        guest_token, _ = self._register_access("replay_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(host, guest)

                first = self._move(host, guest, 7, 3, "replay-b0")
                self.assertEqual(first["request_id"], "replay-b0")

                # 现在轮到白棋：重发黑棋的 request_id 仍应回权威局面，
                # 而不是“还没有轮到你”。坐标故意给非法/占用值，验证
                # 幂等分支排在坐标与占用校验之前。
                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 0, "col": 0},
                        request_id="replay-b0",
                    )
                )
                replay_turn = host.receive_json()
                self.assertEqual(replay_turn["type"], SERVER_GAME_STATE)
                self.assertEqual(replay_turn["request_id"], "replay-b0")
                replay_snapshot = parse_game_state(replay_turn["payload"])
                self.assertEqual(replay_snapshot.move_no, 1)
                self.assertEqual(
                    replay_snapshot.board[7][3].value, "black"
                )
                self.assertIsNone(replay_snapshot.board[0][0])

                # 白棋落子后，(7,3) 已占用且轮到黑棋；重放仍不能报占用。
                self._move(guest, host, 9, 3, "replay-w0")
                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 3},
                        request_id="replay-b0",
                    )
                )
                replay_occupied = host.receive_json()
                self.assertEqual(
                    replay_occupied["type"], SERVER_GAME_STATE
                )
                self.assertEqual(
                    replay_occupied["request_id"], "replay-b0"
                )
                self.assertEqual(
                    parse_game_state(
                        replay_occupied["payload"]
                    ).move_no,
                    2,
                )

                # 黑棋连成五子终局后，重发最后一手仍应回终局快照。
                for index in range(1, 5):
                    self._move(
                        host, guest, 7, 3 + index, f"replay-b{index}"
                    )
                    if index < 4:
                        self._move(
                            guest,
                            host,
                            9,
                            3 + index,
                            f"replay-w{index}",
                        )

                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 0, "col": 0},
                        request_id="replay-b4",
                    )
                )
                final_replay = host.receive_json()
                self.assertEqual(final_replay["type"], SERVER_GAME_STATE)
                self.assertEqual(final_replay["request_id"], "replay-b4")
                final_snapshot = parse_game_state(
                    final_replay["payload"]
                )
                self.assertTrue(final_snapshot.is_game_over)
                self.assertIs(final_snapshot.winner, Player.BLACK)
                self.assertEqual(final_snapshot.move_no, 9)
                self.assertIsNotNone(final_snapshot.last_move)
                self.assertEqual(
                    final_snapshot.last_move.row, 7
                )
                self.assertEqual(
                    final_snapshot.last_move.col, 7
                )


class UndoFailureCleanupTests(_WsTestBase):
    """G13：悔棋写库失败必须清掉请求、恢复计时并继续广播。"""

    def test_undo_accept_db_failure_clears_pending_request(self) -> None:
        host_token, host_id = self._register_access("undofail_host")
        guest_token, _ = self._register_access("undofail_guest")
        manager = self.app.state.room_manager

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect(
                "/api/v1/ws"
            ) as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                game_id = self._open_friend_game(host, guest)
                self._move(host, guest, 7, 7, "undofail-m1")
                self._move(guest, host, 7, 8, "undofail-m2")

                host.send_json(
                    make_message(
                        CLIENT_UNDO_REQUEST,
                        {},
                        request_id="undofail-request",
                    )
                )
                self._receive_type(host, SERVER_UNDO_STATUS)
                self._receive_type(host, SERVER_GAME_STATE)
                self._receive_type(guest, SERVER_UNDO_STATUS)
                self._receive_type(guest, SERVER_GAME_STATE)

                # 让下一次会话创建直接失败，模拟悔棋写库异常。
                original_factory = manager._session_factory

                class _FailingFactory:
                    def __call__(self):
                        manager._session_factory = original_factory
                        raise RuntimeError("模拟数据库故障")

                manager._session_factory = _FailingFactory()
                try:
                    guest.send_json(
                        make_message(
                            CLIENT_UNDO_RESPONSE,
                            {"accept": True},
                            request_id="undofail-accept",
                        )
                    )
                    error = self._receive_type(guest, SERVER_ERROR)
                    self.assertEqual(
                        error["request_id"], "undofail-accept"
                    )
                    self.assertIn(
                        "悔棋失败", error["payload"]["reason"]
                    )
                    host_cancel = self._receive_type(
                        host, SERVER_UNDO_STATUS
                    )
                    guest_cancel = self._receive_type(
                        guest, SERVER_UNDO_STATUS
                    )
                    for status in (host_cancel, guest_cancel):
                        self.assertEqual(
                            status["payload"]["state"], "cancelled"
                        )
                    host_snapshot = parse_game_state(
                        self._receive_type(
                            host, SERVER_GAME_STATE
                        )["payload"]
                    )
                    guest_snapshot = parse_game_state(
                        self._receive_type(
                            guest, SERVER_GAME_STATE
                        )["payload"]
                    )
                finally:
                    manager._session_factory = original_factory

                for snapshot in (host_snapshot, guest_snapshot):
                    self.assertFalse(snapshot.is_game_over)
                    self.assertIsNone(snapshot.winner)
                    self.assertEqual(snapshot.move_no, 2)
                    self.assertEqual(snapshot.undo_requester_id, "")

                room = manager._room_by_user[host_id]
                self.assertIsNone(room.undo_requester_id)
                self.assertIsNone(room.undo_task)
                self.assertIsNotNone(room.turn_deadline_ms)

                # 对局可以继续：悔棋失败不应留下暂停的时钟或挂起状态。
                continued = self._move(
                    host, guest, 8, 8, "undofail-m3"
                )
                self.assertEqual(continued["type"], SERVER_GAME_STATE)
                self.assertEqual(
                    parse_game_state(
                        continued["payload"]
                    ).move_no,
                    3,
                )
                detail = self.client.get(
                    f"/api/v1/games/{game_id}",
                    headers=self.auth_headers(host_token),
                )
                self.assertEqual(detail.status_code, 200, detail.text)


class TimeoutReasonHelperTests(unittest.TestCase):
    """G14：落子补判与计时任务共用同一套超时原因。"""

    def _room(self) -> Room:
        return Room(
            code="TEST",
            game_id="game-1",
            black_user_id="black",
            white_user_id="white",
            black_name="black",
            white_name="white",
            time_control=DEFAULT_TIME_CONTROL,
        )

    def test_disconnected_loser_is_escape_connected_is_timeout(
        self,
    ) -> None:
        manager = _bare_manager()
        room = self._room()
        room.connections["black"] = SimpleNamespace(user_id="black")
        self.assertEqual(
            manager._timeout_reason_locked(room, "black"), "timeout"
        )
        self.assertEqual(
            manager._timeout_reason_locked(room, "white"), "escape"
        )


class MembershipIdentityTests(unittest.TestCase):
    """G15：被替换的旧连接不能认输或关闭房间。"""

    def _manager_and_room(self, status: str):
        manager = _bare_manager()
        manager._connections = {"user": SimpleNamespace(user_id="user")}
        manager._spectator_by_user = {}
        room = Room(
            code="ROOM",
            game_id="game-1",
            black_user_id="user",
            white_user_id="rival",
            black_name="user",
            white_name="rival",
            time_control=DEFAULT_TIME_CONTROL,
            status=status,
        )
        manager._room_by_user = {"user": room}
        manager._rooms_by_code = {room.code: room}
        return manager, room

    def test_stale_connection_cannot_resign(self) -> None:
        manager, room = self._manager_and_room("playing")
        old_conn = manager._connections["user"]
        new_conn = SimpleNamespace(user_id="user", connected=True)
        room.connections = {"user": new_conn}
        room.black_user_id = "user"
        room.white_user_id = "rival"

        asyncio.run(manager.resign(old_conn, "stale-resign"))

        self.assertEqual(room.status, "playing")
        self.assertIsNone(room.winner_user_id)
        self.assertIsNone(room.result_reason)

    def test_stale_connection_cannot_leave_room(self) -> None:
        manager, room = self._manager_and_room("waiting")
        old_conn = manager._connections["user"]
        new_conn = SimpleNamespace(user_id="user", connected=True)
        room.connections = {"user": new_conn}

        asyncio.run(manager.leave_room(old_conn, "stale-leave"))

        self.assertIs(manager._rooms_by_code.get("ROOM"), room)
        self.assertIs(manager._room_by_user.get("user"), room)
        self.assertFalse(hasattr(old_conn, "sent"))


class StateLockDatabaseTests(_WsTestBase):
    """G16：全局 ``_state_lock`` 内不得出现数据库读写。"""

    def test_quick_match_reads_and_writes_db_outside_state_lock(
        self,
    ) -> None:
        first_token, _ = self._register_access("lockmatch_a")
        second_token, _ = self._register_access("lockmatch_b")
        manager = self.app.state.room_manager
        observations: list[tuple[str, bool]] = []

        async def fetch_wrapper(user_id: str):
            observations.append(
                ("fetch", manager._state_lock.locked())
            )
            return await original_fetch(user_id)

        async def create_wrapper(*args, **kwargs):
            observations.append(
                ("create", manager._state_lock.locked())
            )
            return await original_create(*args, **kwargs)

        original_fetch = manager._fetch_user_rating
        original_create = manager._create_game_row
        manager._fetch_user_rating = fetch_wrapper
        manager._create_game_row = create_wrapper
        try:
            with self.client.websocket_connect(
                "/api/v1/ws"
            ) as first, self.client.websocket_connect(
                "/api/v1/ws"
            ) as second:
                self._authenticate(first, first_token)
                self._authenticate(second, second_token)
                first.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                self._receive_type(first, SERVER_QUEUE_STATUS)
                second.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                for ws in (first, second):
                    self._receive_type(ws, SERVER_QUEUE_STATUS)
                    self._receive_type(ws, SERVER_MATCH_FOUND)
                    self._receive_type(ws, SERVER_GAME_STATE)
        finally:
            del manager._fetch_user_rating
            del manager._create_game_row

        kinds = {kind for kind, _ in observations}
        self.assertIn("fetch", kinds)
        self.assertIn("create", kinds)
        for kind, locked in observations:
            self.assertFalse(
                locked, f"{kind} 在 _state_lock 内执行"
            )

    def test_join_room_writes_db_outside_state_lock(self) -> None:
        host_token, _ = self._register_access("lockjoin_host")
        guest_token, _ = self._register_access("lockjoin_guest")
        manager = self.app.state.room_manager
        observations: list[bool] = []

        async def join_wrapper(conn, room):
            observations.append(manager._state_lock.locked())
            return await original_join(conn, room)

        original_join = manager._join_waiting_room
        manager._join_waiting_room = join_wrapper
        try:
            with self.client.websocket_connect(
                "/api/v1/ws"
            ) as host, self.client.websocket_connect(
                "/api/v1/ws"
            ) as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(host, guest)
        finally:
            del manager._join_waiting_room

        self.assertTrue(observations)
        self.assertFalse(any(observations))


class JoinRoomRaceTests(_WsTestBase):
    """G16/S2：两个玩家同时抢白座时只有一人成功。"""

    def _receive_join_outcome(self, ws) -> dict:
        for _ in range(30):
            message = ws.receive_json()
            if message["type"] in (SERVER_GAME_STATE, SERVER_ERROR):
                return message
        self.fail("未收到加入房间的结果")
        return {}

    def test_concurrent_join_only_one_guest_gets_seat(self) -> None:
        host_token, _ = self._register_access("race_host")
        first_token, first_id = self._register_access("race_guest_a")
        second_token, second_id = self._register_access("race_guest_b")
        manager = self.app.state.room_manager

        with self.client.websocket_connect("/api/v1/ws") as host:
            self._authenticate(host, host_token)
            host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
            code = host.receive_json()["payload"]["room_code"]

            with self.client.websocket_connect(
                "/api/v1/ws"
            ) as first, self.client.websocket_connect(
                "/api/v1/ws"
            ) as second:
                self._authenticate(first, first_token)
                self._authenticate(second, second_token)
                # 不交替读取，让两条加入命令在服务端真正交错执行。
                first.send_json(
                    make_message(
                        CLIENT_JOIN_ROOM, {"room_code": code}
                    )
                )
                second.send_json(
                    make_message(
                        CLIENT_JOIN_ROOM, {"room_code": code}
                    )
                )
                outcomes = {
                    first_id: self._receive_join_outcome(first),
                    second_id: self._receive_join_outcome(second),
                }
                # 房主也要收到最终快照（成功者的广播）。
                self._receive_type(host, SERVER_GAME_STATE)

        winners = [
            user_id
            for user_id, message in outcomes.items()
            if message["type"] == SERVER_GAME_STATE
        ]
        losers = [
            user_id
            for user_id, message in outcomes.items()
            if message["type"] == SERVER_ERROR
        ]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len(losers), 1, outcomes)
        self.assertIn(
            outcomes[losers[0]]["payload"]["reason"],
            {"房间已经开始或已满", "服务器无法开始对局"},
        )
        room = manager._room_by_user[winners[0]]
        self.assertEqual(room.white_user_id, winners[0])
        # 失败者不能残留在房间/全局索引里。
        self.assertNotIn(losers[0], manager._room_by_user)
        self.assertNotIn(losers[0], room.connections)


class BroadcastConcurrencyTests(unittest.TestCase):
    """G18：广播并发发送，慢客户端不拖住其他连接。"""

    def _manager(self) -> RoomManager:
        return object.__new__(RoomManager)

    def test_slow_connection_does_not_serialize_broadcast(self) -> None:
        manager = self._manager()

        class _Conn:
            def __init__(self, user_id: str, delay: float, fail=False):
                self.user_id = user_id
                self.delay = delay
                self.fail = fail
                self.sent: list[dict] = []

            async def send(self, message: dict) -> bool:
                await asyncio.sleep(self.delay)
                if self.fail:
                    raise RuntimeError("发送失败")
                self.sent.append(message)
                return True

        fast = _Conn("fast", 0.0)
        slows = [_Conn(f"slow-{index}", 0.2) for index in range(3)]
        broken = _Conn("broken", 0.0, fail=True)
        sends = [(fast, {"type": "a"}), (broken, {"type": "c"})]
        sends.extend((conn, {"type": "b"}) for conn in slows)
        started = time.perf_counter()
        asyncio.run(manager._send_all(sends))
        elapsed = time.perf_counter() - started
        self.assertEqual(fast.sent, [{"type": "a"}])
        for conn in slows:
            self.assertEqual(conn.sent, [{"type": "b"}])
        # 逐个 await 需要 0.6 秒以上；并发发送应接近最慢的一条。
        self.assertLess(elapsed, 0.5)

    def test_per_send_timeout_is_bounded(self) -> None:
        manager = self._manager()

        class _Conn:
            def __init__(self, user_id: str, delay: float):
                self.user_id = user_id
                self.delay = delay
                self.sent: list[dict] = []

            async def send(self, message: dict) -> bool:
                await asyncio.sleep(self.delay)
                self.sent.append(message)
                return True

        fast = _Conn("fast", 0.0)
        slow = _Conn("slow", 5.0)
        original_timeout = (
            rooms_module._BROADCAST_SEND_TIMEOUT_SECONDS
        )
        rooms_module._BROADCAST_SEND_TIMEOUT_SECONDS = 0.05
        try:
            started = time.perf_counter()
            asyncio.run(
                manager._send_all(
                    [(fast, {"type": "a"}), (slow, {"type": "b"})]
                )
            )
            elapsed = time.perf_counter() - started
        finally:
            rooms_module._BROADCAST_SEND_TIMEOUT_SECONDS = (
                original_timeout
            )
        self.assertEqual(fast.sent, [{"type": "a"}])
        self.assertLess(elapsed, 0.5)


class InviteCleanupAndCacheTests(unittest.TestCase):
    """A15：过期邀请惰性清理，好友缓存有容量上限。"""

    def _manager(self) -> RoomManager:
        return _bare_manager()

    def _invite(self, invite_id: str, age: float) -> RoomInvite:
        return RoomInvite(
            invite_id=invite_id,
            room=object(),  # type: ignore[arg-type]
            inviter_id="a",
            invitee_id="b",
            created_at=time.monotonic() - age,
        )

    def test_expired_invites_are_purged_lazily(self) -> None:
        manager = self._manager()
        manager._invites = {
            "expired": self._invite("expired", 61),
            "fresh": self._invite("fresh", 1),
            "untouched": self._invite("untouched", 61),
        }
        purged = manager._purge_expired_invites(
            except_id="untouched"
        )
        self.assertEqual(purged, 1)
        self.assertNotIn("expired", manager._invites)
        self.assertIn("fresh", manager._invites)
        self.assertIn("untouched", manager._invites)

    def test_friend_cache_is_lru_bounded(self) -> None:
        manager = self._manager()
        manager._friend_cache = OrderedDict()
        limit = FRIEND_CACHE_MAX_SIZE
        for index in range(limit + 10):
            manager._friend_cache_put(
                str(index), frozenset({str(index)})
            )
        self.assertEqual(len(manager._friend_cache), limit)
        self.assertNotIn("9", manager._friend_cache)
        self.assertIn("10", manager._friend_cache)
        self.assertIn(str(limit + 9), manager._friend_cache)
        # 读取会刷新 LRU 顺序：刚读过的条目不会被下一条淘汰。
        manager._friend_cache_get("10")
        manager._friend_cache_put("new", frozenset())
        self.assertIn("10", manager._friend_cache)
        self.assertNotIn("11", manager._friend_cache)
        self.assertIn(str(limit + 9), manager._friend_cache)


class ConnectionQuotaReplacementTests(unittest.TestCase):
    """A16：替换失败时不能先丢掉旧连接的配额。"""

    def test_failed_replacement_keeps_old_reservation(self) -> None:
        limiter = IpConnectionLimiter(max_per_ip=1)
        self.assertTrue(limiter.reserve("1.1.1.1", "old"))
        # 替换目标不属于该 IP：净增 1，放不下必须拒绝，且 old 仍在。
        self.assertFalse(
            limiter.reserve(
                "1.1.1.1", "new", replacing_id="ghost"
            )
        )
        self.assertTrue(limiter.reserve("1.1.1.1", "old"))
        self.assertFalse(limiter.reserve("1.1.1.1", "extra"))
        # 正常替换：净增 0，成功后旧连接才释放。
        self.assertTrue(
            limiter.reserve(
                "1.1.1.1", "replacement", replacing_id="old"
            )
        )
        self.assertFalse(limiter.reserve("1.1.1.1", "extra-2"))
        self.assertTrue(limiter.reserve("1.1.1.1", "replacement"))


class RequestIdEchoTests(_WsTestBase):
    """A18：命令错误与解析错误都要回显 request_id。"""

    def test_command_and_parse_errors_echo_request_id(self) -> None:
        token, _ = self._register_access("requestid_user")
        with self.client.websocket_connect("/api/v1/ws") as ws:
            self._authenticate(ws, token)

            # 不在房间中落子：错误响应仍要带回 request_id。
            ws.send_json(
                make_message(
                    CLIENT_MOVE_INTENT,
                    {"row": 7, "col": 7},
                    request_id="move-no-room",
                )
            )
            move_error = ws.receive_json()
            self.assertEqual(move_error["type"], SERVER_ERROR)
            self.assertEqual(
                move_error["request_id"], "move-no-room"
            )

            # 已认证连接再次认证：同样的错误也要能归到请求上。
            ws.send_json(
                make_message(
                    CLIENT_AUTH,
                    {"access_token": token},
                    request_id="reauth-1",
                )
            )
            auth_error = ws.receive_json()
            self.assertEqual(auth_error["type"], SERVER_ERROR)
            self.assertEqual(auth_error["request_id"], "reauth-1")

            # 协议解析失败时，从原始帧里尽量取出 request_id。
            ws.send_json(
                {
                    "protocol": 1,
                    "type": "unknown_command",
                    "payload": {},
                    "request_id": "parse-rid",
                }
            )
            parse_error = ws.receive_json()
            self.assertEqual(parse_error["type"], SERVER_ERROR)
            self.assertEqual(parse_error["request_id"], "parse-rid")
            self.assertIn(
                "未知消息类型", parse_error["payload"]["reason"]
            )


class EndGameTransactionTests(_WsTestBase):
    """S3：终局写库失败回滚内存状态，重试后原子结算。"""

    def _inspect_game(self, game_id: str):
        async def inspect():
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    game_row = await session.get(Game, game_id)
                    events = await session.scalar(
                        select(func.count())
                        .select_from(RatingEvent)
                        .where(RatingEvent.game_id == game_id)
                    )
                    return (
                        game_row.status,
                        bool(game_row.rating_applied),
                        int(events or 0),
                    )
            finally:
                await engine.dispose()

        return asyncio.run(inspect())

    def test_settlement_failure_rolls_back_terminal_state(self) -> None:
        first_token, _ = self._register_access("tx_a")
        second_token, _ = self._register_access("tx_b")
        manager = self.app.state.room_manager

        with self.client.websocket_connect(
            "/api/v1/ws"
        ) as first, self.client.websocket_connect(
            "/api/v1/ws"
        ) as second:
            self._authenticate(first, first_token)
            self._authenticate(second, second_token)
            first.send_json(make_message(CLIENT_QUICK_MATCH, {}))
            self._receive_type(first, SERVER_QUEUE_STATUS)
            second.send_json(make_message(CLIENT_QUICK_MATCH, {}))
            for ws in (first, second):
                self._receive_type(ws, SERVER_QUEUE_STATUS)
                self._receive_type(ws, SERVER_MATCH_FOUND)
            first_state = parse_game_state(
                self._receive_type(
                    first, SERVER_GAME_STATE
                )["payload"]
            )
            second_state = parse_game_state(
                self._receive_type(
                    second, SERVER_GAME_STATE
                )["payload"]
            )
            self.assertEqual(first_state.game_id, second_state.game_id)
            game_id = first_state.game_id
            if first_state.your_color is Player.BLACK:
                black_ws = first
            else:
                black_ws = second

            async def boom(*args, **kwargs):
                raise RuntimeError("模拟结算失败")

            # 结算逻辑由 RatingSettlement 服务持有，直接替换服务方法。
            manager._settlement.settle_rated_game_in_session = boom
            try:
                black_ws.send_json(make_message(CLIENT_RESIGN, {}))
                failed_first = parse_game_state(
                    self._receive_type(
                        first, SERVER_GAME_STATE
                    )["payload"]
                )
                failed_second = parse_game_state(
                    self._receive_type(
                        second, SERVER_GAME_STATE
                    )["payload"]
                )
            finally:
                del manager._settlement.settle_rated_game_in_session

            for snapshot in (failed_first, failed_second):
                self.assertFalse(snapshot.is_game_over)
                self.assertEqual(snapshot.move_no, 0)
                self.assertIsNone(snapshot.winner)
                self.assertEqual(snapshot.result_reason, "")
            status, applied, events = self._inspect_game(game_id)
            self.assertEqual(status, "playing")
            self.assertFalse(applied)
            self.assertEqual(events, 0)

            # 数据库恢复后再次认输：终局与积分应在一次提交里同时生效。
            black_ws.send_json(make_message(CLIENT_RESIGN, {}))
            ended_first = parse_game_state(
                self._receive_type(
                    first, SERVER_GAME_STATE
                )["payload"]
            )
            ended_second = parse_game_state(
                self._receive_type(
                    second, SERVER_GAME_STATE
                )["payload"]
            )
            self.assertTrue(ended_first.is_game_over)
            self.assertTrue(ended_second.is_game_over)
            self.assertIsNotNone(ended_first.winner)

        status, applied, events = self._inspect_game(game_id)
        self.assertNotEqual(status, "playing")
        self.assertTrue(applied)
        self.assertEqual(events, 2)


class WinningMoveTransactionTests(_WsTestBase):
    """S3：五连终局的落子、终局状态与积分结算必须在一次提交里生效。"""

    def _open_ranked_game(
        self, first, second, first_token: str, second_token: str
    ) -> tuple:
        """匹配一局排位，返回 (执黑连接, 执白连接, game_id)。"""
        self._authenticate(first, first_token)
        self._authenticate(second, second_token)
        first.send_json(make_message(CLIENT_QUICK_MATCH, {}))
        self._receive_type(first, SERVER_QUEUE_STATUS)
        second.send_json(make_message(CLIENT_QUICK_MATCH, {}))
        snapshots = []
        for ws in (first, second):
            self._receive_type(ws, SERVER_QUEUE_STATUS)
            self._receive_type(ws, SERVER_MATCH_FOUND)
            snapshots.append(
                parse_game_state(
                    self._receive_type(ws, SERVER_GAME_STATE)["payload"]
                )
            )
        first_state, second_state = snapshots
        self.assertEqual(first_state.game_id, second_state.game_id)
        if first_state.your_color is Player.BLACK:
            return first, second, first_state.game_id
        return second, first, first_state.game_id

    def _seed_near_win(self, manager: RoomManager, black_id: str) -> Room:
        """把内存棋局摆成黑棋四连、轮到黑棋；数据库仍停在 move_no=0。"""
        room = manager._room_by_user[black_id]
        for col in range(4):
            self.assertTrue(room.game.place_stone(7, col))
            self.assertTrue(room.game.place_stone(8, col))
        self.assertIs(room.game.current_player, Player.BLACK)
        self.assertEqual(room.game.move_count, 8)
        return room

    def _inspect_game(self, game_id: str) -> dict:
        async def inspect() -> dict:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    game_row = await session.get(Game, game_id)
                    self.assertIsNotNone(game_row)
                    assert game_row is not None
                    move_count = await session.scalar(
                        select(func.count())
                        .select_from(Move)
                        .where(Move.game_id == game_id)
                    )
                    last_ply_move_count = await session.scalar(
                        select(func.count())
                        .select_from(Move)
                        .where(
                            Move.game_id == game_id,
                            Move.move_no == 9,
                        )
                    )
                    events = (
                        await session.scalars(
                            select(RatingEvent).where(
                                RatingEvent.game_id == game_id
                            )
                        )
                    ).all()
                    user_ids = (
                        game_row.black_user_id,
                        game_row.white_user_id,
                    )
                    ratings = {
                        user.id: user.rating
                        for user in (
                            await session.scalars(
                                select(User).where(User.id.in_(user_ids))
                            )
                        ).all()
                    }
                    return {
                        "status": game_row.status,
                        "move_no": game_row.move_no,
                        "rating_applied": bool(game_row.rating_applied),
                        "winner_user_id": game_row.winner_user_id,
                        "move_count": int(move_count or 0),
                        "last_ply_move_count": int(
                            last_ply_move_count or 0
                        ),
                        "event_users": {
                            event.user_id for event in events
                        },
                        "ratings": ratings,
                    }
            finally:
                await engine.dispose()

        return asyncio.run(inspect())

    def test_settlement_failure_rolls_back_winning_move_then_retry(
        self,
    ) -> None:
        first_token, first_id = self._register_access("win_tx_first")
        second_token, second_id = self._register_access(
            "win_tx_second"
        )
        manager = self.app.state.room_manager

        with self.client.websocket_connect(
            "/api/v1/ws"
        ) as first, self.client.websocket_connect(
            "/api/v1/ws"
        ) as second:
            black_ws, white_ws, game_id = self._open_ranked_game(
                first, second, first_token, second_token
            )
            room = manager._room_by_user[first_id]
            black_id = room.black_user_id
            white_id = room.white_user_id
            self.assertEqual({black_id, white_id}, {first_id, second_id})
            self._seed_near_win(manager, black_id)

            async def boom(*args, **kwargs):
                raise RuntimeError("模拟结算失败")

            manager._settlement.settle_rated_game_in_session = boom
            try:
                black_ws.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 4},
                        request_id="win-atomic",
                    )
                )
                error = self._receive_type(black_ws, SERVER_ERROR)
            finally:
                del manager._settlement.settle_rated_game_in_session

            self.assertEqual(error["request_id"], "win-atomic")
            self.assertIn(
                "服务器保存落子失败", error["payload"]["reason"]
            )

            # 内存落子已撤销，房间仍在进行中，重试请求没有被记为已处理。
            self.assertEqual(room.status, "playing")
            self.assertIsNone(room.winner_user_id)
            self.assertFalse(room.rating_applied)
            self.assertNotIn("win-atomic", room.processed_moves)
            self.assertEqual(room.game.move_count, 8)
            self.assertIsNone(room.game.board[7][4])
            self.assertIsNone(room.game.winner)

            black_conn = manager._connections[black_id]
            snapshot_message = manager._game_state_message(
                room, black_conn
            )
            self.assertIsNotNone(snapshot_message)
            assert snapshot_message is not None
            self.assertEqual(
                snapshot_message["payload"]["status"], "playing"
            )
            failed_snapshot = parse_game_state(
                snapshot_message["payload"]
            )
            self.assertFalse(failed_snapshot.is_game_over)
            self.assertIsNone(failed_snapshot.board[7][4])
            self.assertEqual(failed_snapshot.move_no, 8)

            # 数据库整体回滚：终局状态、落子与积分都没写进去。
            failed_db = self._inspect_game(game_id)
            self.assertEqual(failed_db["status"], "playing")
            self.assertEqual(failed_db["move_no"], 0)
            self.assertFalse(failed_db["rating_applied"])
            self.assertIsNone(failed_db["winner_user_id"])
            self.assertEqual(failed_db["move_count"], 0)
            self.assertEqual(failed_db["last_ply_move_count"], 0)
            self.assertEqual(failed_db["event_users"], set())
            self.assertEqual(failed_db["ratings"][black_id], 1200)
            self.assertEqual(failed_db["ratings"][white_id], 1200)

            # 同一个 request_id 重试：落子、终局与结算一次提交成功。
            black_ws.send_json(
                make_message(
                    CLIENT_MOVE_INTENT,
                    {"row": 7, "col": 4},
                    request_id="win-atomic",
                )
            )
            black_message = self._receive_type(
                black_ws, SERVER_GAME_STATE
            )
            white_message = self._receive_type(
                white_ws, SERVER_GAME_STATE
            )
            self.assertEqual(black_message["request_id"], "win-atomic")
            black_end = parse_game_state(black_message["payload"])
            white_end = parse_game_state(white_message["payload"])
            for snapshot in (black_end, white_end):
                self.assertTrue(snapshot.is_game_over)
                self.assertIs(snapshot.winner, Player.BLACK)
                self.assertEqual(snapshot.move_no, 9)
                self.assertEqual(
                    snapshot.board[7][4].value, "black"
                )
            self.assertGreater(black_end.rating_delta or 0, 0)
            self.assertLess(white_end.rating_delta or 0, 0)
            self.assertEqual(black_end.your_rating_before, 1200)
            self.assertTrue(room.rating_applied)
            self.assertEqual(room.status, "black_won")

        applied_db = self._inspect_game(game_id)
        self.assertEqual(applied_db["status"], "black_won")
        self.assertEqual(applied_db["move_no"], 9)
        self.assertTrue(applied_db["rating_applied"])
        self.assertEqual(applied_db["winner_user_id"], black_id)
        self.assertEqual(applied_db["move_count"], 1)
        self.assertEqual(applied_db["last_ply_move_count"], 1)
        self.assertEqual(applied_db["event_users"], {black_id, white_id})
        self.assertGreater(applied_db["ratings"][black_id], 1200)
        self.assertLess(applied_db["ratings"][white_id], 1200)

    def test_winning_move_does_not_call_locked_settlement(self) -> None:
        first_token, _ = self._register_access("guard_tx_first")
        second_token, second_id = self._register_access(
            "guard_tx_second"
        )
        manager = self.app.state.room_manager

        async def boom(*args, **kwargs):
            raise AssertionError("五连终局不应再调用独立事务结算")

        with self.client.websocket_connect(
            "/api/v1/ws"
        ) as first, self.client.websocket_connect(
            "/api/v1/ws"
        ) as second:
            black_ws, white_ws, game_id = self._open_ranked_game(
                first, second, first_token, second_token
            )
            room = manager._room_by_user[second_id]
            black_id = room.black_user_id
            white_id = room.white_user_id
            self._seed_near_win(manager, black_id)

            # 独立事务结算无论是门面方法还是底层服务方法都不应被调用。
            manager._settle_rated_game_locked = boom
            manager._settlement.settle_rated_game_locked = boom
            try:
                black_ws.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 4},
                        request_id="guard-win",
                    )
                )
                black_end = parse_game_state(
                    self._receive_type(
                        black_ws, SERVER_GAME_STATE
                    )["payload"]
                )
                white_end = parse_game_state(
                    self._receive_type(
                        white_ws, SERVER_GAME_STATE
                    )["payload"]
                )
            finally:
                del manager._settle_rated_game_locked
                del manager._settlement.settle_rated_game_locked

            for snapshot in (black_end, white_end):
                self.assertTrue(snapshot.is_game_over)
                self.assertIs(snapshot.winner, Player.BLACK)
                self.assertEqual(snapshot.move_no, 9)
            self.assertGreater(black_end.rating_delta or 0, 0)

        guard_db = self._inspect_game(game_id)
        self.assertEqual(guard_db["status"], "black_won")
        self.assertTrue(guard_db["rating_applied"])
        self.assertEqual(guard_db["move_count"], 1)
        self.assertEqual(guard_db["event_users"], {black_id, white_id})


class RepairUnsettledGamesTests(_WsTestBase):
    """S3 兜底：已终局但未结算的排位对局能在启动扫描中补结算。"""

    def test_terminal_unsettled_game_is_repaired(self) -> None:
        _, black_id = self._register_access("repair_black")
        _, white_id = self._register_access("repair_white")

        async def seed_and_repair() -> tuple[int, bool, int]:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            game_id = new_id()
            try:
                async with factory() as session:
                    session.add(
                        Game(
                            id=game_id,
                            room_code="REPAIR01",
                            black_user_id=black_id,
                            white_user_id=white_id,
                            status="black_won",
                            winner_user_id=black_id,
                            result_reason="five",
                            move_no=9,
                            turn_seconds=30,
                            rated=True,
                            rating_applied=False,
                        )
                    )
                    await session.commit()
                manager = RoomManager(
                    self.settings, factory, ServerMetrics()
                )
                repaired = await manager.repair_unsettled_games()
                async with factory() as session:
                    game_row = await session.get(Game, game_id)
                    events = await session.scalar(
                        select(func.count())
                        .select_from(RatingEvent)
                        .where(RatingEvent.game_id == game_id)
                    )
                    return (
                        repaired,
                        bool(game_row.rating_applied),
                        int(events or 0),
                    )
            finally:
                await engine.dispose()

        repaired, applied, events = asyncio.run(seed_and_repair())
        self.assertEqual(repaired, 1)
        self.assertTrue(applied)
        self.assertEqual(events, 2)

    def test_unused_playing_row_is_abandoned(self) -> None:
        """匹配写库成功但二次校验失败时，不能留下 playing 孤儿行。"""
        _, black_id = self._register_access("abandon_black")
        _, white_id = self._register_access("abandon_white")

        async def seed_and_abandon() -> tuple[str, str | None]:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            game_id = new_id()
            try:
                async with factory() as session:
                    session.add(
                        Game(
                            id=game_id,
                            room_code="ABANDON1",
                            black_user_id=black_id,
                            white_user_id=white_id,
                            status="playing",
                            rated=True,
                        )
                    )
                    await session.commit()
                manager = RoomManager(
                    self.settings, factory, ServerMetrics()
                )
                await manager._abandon_game_row(game_id, "left")
                async with factory() as session:
                    row = await session.get(Game, game_id)
                    return row.status, row.result_reason
            finally:
                await engine.dispose()

        status, reason = asyncio.run(seed_and_abandon())
        self.assertEqual(status, "abandoned")
        self.assertEqual(reason, "left")


if __name__ == "__main__":
    unittest.main(verbosity=2)
