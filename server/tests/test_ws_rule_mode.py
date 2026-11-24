"""规则模式（freestyle / renju）的服务端端到端测试。

覆盖 A1：
* 自建房可选择规则模式，建房回执、权威快照与数据库对局行保持一致；
* 缺省与显式 freestyle 都按自由规则处理，非法值被拒绝且不落库；
* 连珠模式下黑棋四四禁手由服务器拒绝，黑棋恰好五连仍获胜，
  白棋长连获胜；
* 自由规则下同样的四四/长连棋形不受禁手限制；
* 再战沿用原规则模式，排位匹配固定自由规则；
* 对战广场列表与对局详情暴露 rule_mode；
* 终局前查询赛季失败时撤销内存落子且不写库（S3 加固）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from sqlalchemy import func, select

from gomoku.shared.game_model import GameModel
from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_QUICK_MATCH,
    CLIENT_REMATCH,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_MOVE_REJECTED,
    SERVER_QUEUE_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CREATED,
    make_message,
    parse_game_state,
)
from gomoku.shared.rules import Player, RuleMode
from gomoku.shared.time_control import DEFAULT_TIME_CONTROL
from server.app.db import create_engine, create_session_factory
from server.app.metrics import ServerMetrics
from server.app.models import Game, Move
from server.app.ws import payloads
from server.app.ws.entities import ClientConnection, Room, RoomInvite
from server.app.ws.rooms import RoomManager
from server.tests.helpers import ServerTestBase


class _RuleModeTestBase(ServerTestBase):
    def _register_access(self, username: str) -> tuple[str, str]:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        return data["access_token"], data["user"]["id"]

    def _authenticate(self, ws, token: str) -> None:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)

    def _receive_type(self, ws, expected: str, attempts: int = 30) -> dict:
        for _ in range(attempts):
            message = ws.receive_json()
            if message["type"] == expected:
                return message
        self.fail(f"未收到期望消息 {expected}")
        return {}

    def _open_friend_game(
        self, host, guest, payload: dict | None = None
    ) -> tuple[dict, dict]:
        """建房并加入；返回 (建房回执, 房主收到首份快照的消息)。"""
        host.send_json(make_message(CLIENT_CREATE_ROOM, payload or {}))
        created = self._receive_type(host, SERVER_ROOM_CREATED)
        code = created["payload"]["room_code"]
        guest.send_json(
            make_message(CLIENT_JOIN_ROOM, {"room_code": code})
        )
        host_state = self._receive_type(host, SERVER_GAME_STATE)
        guest_state = self._receive_type(guest, SERVER_GAME_STATE)
        self.assertEqual(
            host_state["payload"]["game_id"],
            guest_state["payload"]["game_id"],
        )
        return created, host_state

    def _open_ranked_game(
        self,
        first,
        second,
        first_token: str,
        second_token: str,
        first_payload: dict | None = None,
    ) -> tuple:
        """匹配一局排位，返回 (执黑连接, 执白连接, game_id)。"""
        self._authenticate(first, first_token)
        self._authenticate(second, second_token)
        first.send_json(
            make_message(CLIENT_QUICK_MATCH, first_payload or {})
        )
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

    def _move(
        self, sender, other, row: int, col: int, request_id: str
    ) -> dict:
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

    def _play_black_double_four(self, host, guest) -> None:
        """摆出黑棋在 (7,6) 同时形成横向与纵向四的合法局面。"""
        moves = [
            ("black", 7, 3),
            ("white", 0, 0),
            ("black", 4, 6),
            ("white", 0, 2),
            ("black", 7, 4),
            ("white", 0, 4),
            ("black", 5, 6),
            ("white", 0, 6),
            ("black", 7, 5),
            ("white", 0, 8),
            ("black", 6, 6),
            ("white", 0, 10),
        ]
        for index, (who, row, col) in enumerate(moves):
            sender, other = (
                (host, guest) if who == "black" else (guest, host)
            )
            message = self._move(
                sender, other, row, col, f"df-{who}-{index}"
            )
            self.assertEqual(message["type"], SERVER_GAME_STATE)

    def _game_rows(self) -> list[dict]:
        """直接读库：每个对局行的状态、步数与规则模式。"""

        async def inspect() -> list[dict]:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    games = (await session.scalars(select(Game))).all()
                    return [
                        {
                            "id": game.id,
                            "status": game.status,
                            "rule_mode": game.rule_mode,
                            "move_no": game.move_no,
                            "rating_applied": bool(
                                game.rating_applied
                            ),
                        }
                        for game in games
                    ]
            finally:
                await engine.dispose()

        return asyncio.run(inspect())

    def _move_count(self, game_id: str) -> int:
        """直接读库：某局已持久化的落子行数。"""

        async def inspect() -> int:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    count = await session.scalar(
                        select(func.count())
                        .select_from(Move)
                        .where(Move.game_id == game_id)
                    )
                    return int(count or 0)
            finally:
                await engine.dispose()

        return asyncio.run(inspect())


class ManagerCreateRoomValidationTests(unittest.TestCase):
    """管理器内部入口也必须拒绝非法规则模式（不创建房间或对局行）。"""

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def send_json(self, message: dict) -> None:
            self.messages.append(message)

    def test_invalid_rule_mode_sends_error_without_creating_room(
        self,
    ) -> None:
        settings = SimpleNamespace(max_connections_per_ip=200)
        manager = RoomManager(
            settings, None, ServerMetrics()  # type: ignore[arg-type]
        )
        websocket = self._FakeWebSocket()
        conn = ClientConnection(
            connection_id="cid",
            user_id="user-1",
            username="user",
            websocket=websocket,  # type: ignore[arg-type]
        )
        manager._connections = {"user-1": conn}

        asyncio.run(
            manager.create_room(conn, "bad-mode", rule_mode="chaos")
        )

        self.assertEqual(manager._rooms_by_code, {})
        self.assertEqual(len(websocket.messages), 1)
        message = websocket.messages[0]
        self.assertEqual(message["type"], SERVER_ERROR)
        self.assertEqual(message["request_id"], "bad-mode")
        self.assertEqual(message["payload"]["reason"], "规则模式非法")


class CreateRoomRuleModeTests(_RuleModeTestBase):
    """建房：选择、缺省与非法规则模式。"""

    def test_renju_room_exposes_mode_in_created_state_and_db(self) -> None:
        host_token, _ = self._register_access("rm_renju_host")
        guest_token, _ = self._register_access("rm_renju_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                created, host_state = self._open_friend_game(
                    host, guest, {"rule_mode": "renju"}
                )
                self.assertEqual(created["payload"]["rule_mode"], "renju")
                snapshot = parse_game_state(host_state["payload"])
                self.assertIs(snapshot.rule_mode, RuleMode.RENJU)
                rows = self._game_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["id"], snapshot.game_id)
                self.assertEqual(rows[0]["rule_mode"], "renju")
                self.assertEqual(rows[0]["status"], "playing")

    def test_omitted_rule_mode_defaults_to_freestyle(self) -> None:
        host_token, _ = self._register_access("rm_default_host")
        guest_token, _ = self._register_access("rm_default_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                created, host_state = self._open_friend_game(host, guest)
                self.assertEqual(
                    created["payload"]["rule_mode"], "freestyle"
                )
                snapshot = parse_game_state(host_state["payload"])
                self.assertIs(snapshot.rule_mode, RuleMode.FREESTYLE)
                self.assertEqual(
                    self._game_rows()[0]["rule_mode"], "freestyle"
                )

    def test_invalid_rule_mode_rejected_without_room_or_game_row(
        self,
    ) -> None:
        token, _ = self._register_access("rm_bad_mode")
        manager = self.app.state.room_manager
        with self.client.websocket_connect("/api/v1/ws") as ws:
            self._authenticate(ws, token)
            ws.send_json(
                make_message(
                    CLIENT_CREATE_ROOM,
                    {"rule_mode": "chaos"},
                    request_id="bad-mode",
                )
            )
            error = self._receive_type(ws, SERVER_ERROR)
            self.assertEqual(error["request_id"], "bad-mode")
            self.assertEqual(error["payload"]["reason"], "规则模式非法")
            self.assertEqual(manager._rooms_by_code, {})
            self.assertEqual(manager._room_by_user, {})
            self.assertEqual(self._game_rows(), [])


class RenjuEnforcementTests(_RuleModeTestBase):
    """连珠规则：禁手拒绝、黑棋恰好五连、白棋长连。"""

    def test_black_double_four_rejected(self) -> None:
        host_token, host_id = self._register_access("renju_df_host")
        guest_token, _ = self._register_access("renju_df_guest")
        manager = self.app.state.room_manager
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(
                    host, guest, {"rule_mode": "renju"}
                )
                self._play_black_double_four(host, guest)

                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 6},
                        request_id="renju-forbidden",
                    )
                )
                rejected = self._receive_type(
                    host, SERVER_MOVE_REJECTED
                )
                self.assertEqual(
                    rejected["request_id"], "renju-forbidden"
                )
                self.assertEqual(
                    rejected["payload"]["reason"], "连珠禁手"
                )

                room = manager._room_by_user[host_id]
                self.assertEqual(room.game.move_count, 12)
                self.assertIsNone(room.game.board[7][6])
                self.assertIs(room.game.current_player, Player.BLACK)
                self.assertNotIn("renju-forbidden", room.processed_moves)
                self.assertEqual(self._move_count(room.game_id), 12)

    def test_black_exact_five_still_wins(self) -> None:
        host_token, _ = self._register_access("renju_five_host")
        guest_token, _ = self._register_access("renju_five_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(
                    host, guest, {"rule_mode": "renju"}
                )

                for index, col in enumerate([3, 4, 5, 6]):
                    message = self._move(
                        host, guest, 7, col, f"five-b-{index}"
                    )
                    self.assertFalse(
                        parse_game_state(
                            message["payload"]
                        ).is_game_over
                    )
                    self._move(
                        guest, host, 0, index * 2, f"five-w-{index}"
                    )

                final = self._move(host, guest, 7, 7, "five-b-final")
                snapshot = parse_game_state(final["payload"])
                self.assertEqual(final["payload"]["status"], "black_won")
                self.assertIs(snapshot.winner, Player.BLACK)
                self.assertEqual(snapshot.move_no, 9)
                self.assertEqual(len(snapshot.winning_line), 5)

    def test_white_overline_wins(self) -> None:
        host_token, _ = self._register_access("renju_over_host")
        guest_token, _ = self._register_access("renju_over_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(
                    host, guest, {"rule_mode": "renju"}
                )

                black_spots = [(14, 0), (14, 2), (14, 4), (14, 6), (14, 8)]
                white_spots = [(0, 0), (0, 1), (0, 2), (0, 3), (0, 5)]
                for index in range(5):
                    self._move(
                        host,
                        guest,
                        *black_spots[index],
                        f"over-b-{index}",
                    )
                    self._move(
                        guest,
                        host,
                        *white_spots[index],
                        f"over-w-{index}",
                    )
                last_black = self._move(host, guest, 14, 10, "over-b-last")
                self.assertEqual(last_black["type"], SERVER_GAME_STATE)

                final = self._move(guest, host, 0, 4, "over-w-win")
                snapshot = parse_game_state(final["payload"])
                self.assertEqual(
                    final["payload"]["status"], "white_won"
                )
                self.assertIs(snapshot.winner, Player.WHITE)
                self.assertEqual(snapshot.move_no, 12)
                self.assertEqual(len(snapshot.winning_line), 6)


class FreestyleRuleTests(_RuleModeTestBase):
    """自由规则：同样的禁手/长连棋形不被限制。"""

    def test_black_double_four_point_is_allowed(self) -> None:
        host_token, _ = self._register_access("free_df_host")
        guest_token, _ = self._register_access("free_df_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(host, guest)
                self._play_black_double_four(host, guest)

                message = self._move(
                    host, guest, 7, 6, "free-double-four"
                )
                self.assertEqual(message["type"], SERVER_GAME_STATE)
                snapshot = parse_game_state(message["payload"])
                self.assertFalse(snapshot.is_game_over)
                self.assertEqual(snapshot.move_no, 13)
                self.assertEqual(snapshot.board[7][6].value, "black")
                self.assertIs(snapshot.rule_mode, RuleMode.FREESTYLE)

    def test_black_overline_wins(self) -> None:
        host_token, _ = self._register_access("free_over_host")
        guest_token, _ = self._register_access("free_over_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(
                    host, guest, {"rule_mode": "freestyle"}
                )

                black_spots = [(14, 0), (14, 1), (14, 2), (14, 3), (14, 5)]
                white_spots = [(0, 0), (0, 2), (0, 4), (0, 6), (0, 8)]
                for index in range(5):
                    self._move(
                        host,
                        guest,
                        *black_spots[index],
                        f"free-b-{index}",
                    )
                    self._move(
                        guest,
                        host,
                        *white_spots[index],
                        f"free-w-{index}",
                    )

                final = self._move(host, guest, 14, 4, "free-win")
                snapshot = parse_game_state(final["payload"])
                self.assertEqual(final["payload"]["status"], "black_won")
                self.assertIs(snapshot.winner, Player.BLACK)
                self.assertEqual(snapshot.move_no, 11)
                self.assertEqual(len(snapshot.winning_line), 6)


class RematchRuleModeTests(_RuleModeTestBase):
    def test_rematch_keeps_renju_in_payload_and_new_game_row(self) -> None:
        host_token, _ = self._register_access("rm_rematch_host")
        guest_token, _ = self._register_access("rm_rematch_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_friend_game(
                    host, guest, {"rule_mode": "renju"}
                )

                host.send_json(make_message(CLIENT_RESIGN, {}))
                host_end = self._receive_type(host, SERVER_GAME_STATE)
                guest_end = self._receive_type(guest, SERVER_GAME_STATE)
                self.assertTrue(
                    parse_game_state(host_end["payload"]).is_game_over
                )
                self.assertTrue(
                    parse_game_state(guest_end["payload"]).is_game_over
                )

                host.send_json(make_message(CLIENT_REMATCH, {}))
                self._receive_type(host, SERVER_REMATCH_STATUS)
                self._receive_type(guest, SERVER_REMATCH_STATUS)
                guest.send_json(make_message(CLIENT_REMATCH, {}))

                host_new = self._receive_type(host, SERVER_GAME_STATE)
                guest_new = self._receive_type(guest, SERVER_GAME_STATE)
                for message in (host_new, guest_new):
                    self.assertEqual(
                        message["payload"]["rule_mode"], "renju"
                    )
                    snapshot = parse_game_state(message["payload"])
                    self.assertIs(snapshot.rule_mode, RuleMode.RENJU)
                    self.assertEqual(snapshot.move_no, 0)
                    self.assertFalse(snapshot.is_game_over)

                rows = self._game_rows()
                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    {row["rule_mode"] for row in rows}, {"renju"}
                )
                self.assertEqual(
                    {row["status"] for row in rows},
                    {"white_won", "playing"},
                )


class RankedRuleModeTests(_RuleModeTestBase):
    def test_quick_match_stays_freestyle_even_if_asked(self) -> None:
        first_token, _ = self._register_access("ranked_rm_first")
        second_token, _ = self._register_access("ranked_rm_second")
        with self.client.websocket_connect(
            "/api/v1/ws"
        ) as first, self.client.websocket_connect(
            "/api/v1/ws"
        ) as second:
            _, _, game_id = self._open_ranked_game(
                first,
                second,
                first_token,
                second_token,
                # 排位赛本迭代固定自由规则：客户端请求连珠也不生效。
                {"rule_mode": "renju"},
            )
            manager = self.app.state.room_manager
            room = next(
                room
                for room in manager._rooms_by_code.values()
                if room.game_id == game_id
            )
            self.assertIs(room.rule_mode, RuleMode.FREESTYLE)
            self.assertIs(room.game.rule_mode, RuleMode.FREESTYLE)
            rows = self._game_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["rule_mode"], "freestyle")


class LiveAndDetailRuleModeTests(_RuleModeTestBase):
    def test_live_list_and_game_detail_expose_rule_mode(self) -> None:
        host_token, _ = self._register_access("rm_live_host")
        guest_token, _ = self._register_access("rm_live_guest")
        api_token, _ = self._register_access("rm_live_api")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                _, host_state = self._open_friend_game(
                    host, guest, {"rule_mode": "renju"}
                )
                game_id = host_state["payload"]["game_id"]

                live = self.client.get(
                    "/api/v1/games/live",
                    headers=self.auth_headers(api_token),
                )
                self.assertEqual(live.status_code, 200, live.text)
                entries = live.json()["entries"]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["game_id"], game_id)
                self.assertEqual(entries[0]["rule_mode"], "renju")

                detail = self.client.get(
                    f"/api/v1/games/{game_id}",
                    headers=self.auth_headers(host_token),
                )
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["rule_mode"], "renju")


class InvitePayloadRuleModeTests(unittest.TestCase):
    """邀请载荷在加入前就告诉被邀请者本局规则。"""

    def _room(self, mode: RuleMode) -> Room:
        return Room(
            code="RM1",
            game_id="game-rm-1",
            black_user_id="black-id",
            white_user_id=None,
            black_name="Black",
            white_name="",
            time_control=DEFAULT_TIME_CONTROL,
            rule_mode=mode,
            game=GameModel(mode),
        )

    def test_invite_payload_carries_room_rule_mode(self) -> None:
        for mode, text in (
            (RuleMode.FREESTYLE, "freestyle"),
            (RuleMode.RENJU, "renju"),
        ):
            invite = RoomInvite(
                invite_id="invite-rm",
                room=self._room(mode),
                inviter_id="black-id",
                invitee_id="white-id",
                created_at=0.0,
            )
            payload = payloads.invite_payload(invite, None)
            self.assertEqual(payload["rule_mode"], text)


class WinningMoveSeasonFailureTests(_RuleModeTestBase):
    """S3 加固：取赛季失败也要撤销内存落子且不写库。"""

    def _seed_near_win(self, manager, black_id: str) -> Room:
        """把内存棋局摆成黑棋四连、轮到黑棋；数据库仍停在 move_no=0。"""
        room = manager._room_by_user[black_id]
        for col in range(4):
            self.assertTrue(room.game.place_stone(7, col))
            self.assertTrue(room.game.place_stone(8, col))
        self.assertIs(room.game.current_player, Player.BLACK)
        self.assertEqual(room.game.move_count, 8)
        return room

    def test_season_lookup_failure_rolls_back_winning_move(self) -> None:
        first_token, first_id = self._register_access("season_fail_first")
        second_token, _ = self._register_access("season_fail_second")
        manager = self.app.state.room_manager

        with self.client.websocket_connect(
            "/api/v1/ws"
        ) as first, self.client.websocket_connect(
            "/api/v1/ws"
        ) as second:
            black_ws, _white_ws, game_id = self._open_ranked_game(
                first, second, first_token, second_token
            )
            room = manager._room_by_user[first_id]
            black_id = room.black_user_id
            self._seed_near_win(manager, black_id)

            async def boom(*args, **kwargs):
                raise RuntimeError("模拟赛季查询失败")

            manager._settlement.settlement_season_id = boom
            try:
                black_ws.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 4},
                        request_id="season-fail",
                    )
                )
                error = self._receive_type(black_ws, SERVER_ERROR)
            finally:
                del manager._settlement.settlement_season_id

            self.assertEqual(error["request_id"], "season-fail")
            self.assertIn(
                "服务器保存落子失败", error["payload"]["reason"]
            )

            # 内存落子已撤销，请求没有被记为已处理。
            self.assertEqual(room.status, "playing")
            self.assertIsNone(room.winner_user_id)
            self.assertFalse(room.rating_applied)
            self.assertNotIn("season-fail", room.processed_moves)
            self.assertEqual(room.game.move_count, 8)
            self.assertIsNone(room.game.board[7][4])
            self.assertIsNone(room.game.winner)

            # 数据库什么都没写：没有落子、终局或积分变化。
            rows = self._game_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], game_id)
            self.assertEqual(rows[0]["status"], "playing")
            self.assertEqual(rows[0]["move_no"], 0)
            self.assertFalse(rows[0]["rating_applied"])
            self.assertEqual(self._move_count(game_id), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
