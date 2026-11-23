"""总时长/Fischer 时钟与匹配时限测试。"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CANCEL_MATCH,
    CLIENT_CREATE_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_QUICK_MATCH,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_QUEUE_STATUS,
    make_message,
    parse_game_state,
)
from gomoku.shared.time_control import TimeControl
from server.app.ws.entities import Room
from server.tests.helpers import ServerTestBase


class FischerMatchTests(ServerTestBase):
    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def _receive_type(self, ws, expected: str, attempts: int = 10) -> dict:
        for _ in range(attempts):
            message = ws.receive_json()
            if message["type"] == expected:
                return message
        self.fail(f"未收到期望消息 {expected}")
        return {}

    def _match_fischer(self, main_seconds: int = 300, increment: int = 3):
        first_token = self._register_access("fischer_a")
        second_token = self._register_access("fischer_b")
        control = {
            "mode": "fischer",
            "main_seconds": main_seconds,
            "increment_seconds": increment,
        }
        return first_token, second_token, control

    def test_fischer_match_exposes_clocks_and_increment(self) -> None:
        first_token, second_token, control = self._match_fischer()
        with self.client.websocket_connect("/api/v1/ws") as first:
            with self.client.websocket_connect("/api/v1/ws") as second:
                self._authenticate(first, first_token)
                self._authenticate(second, second_token)
                first.send_json(
                    make_message(
                        CLIENT_QUICK_MATCH, {"time_control": control}
                    )
                )
                self.assertEqual(
                    first.receive_json()["payload"]["state"], "queued"
                )
                second.send_json(
                    make_message(
                        CLIENT_QUICK_MATCH, {"time_control": control}
                    )
                )
                first_found = self._receive_type(first, SERVER_MATCH_FOUND)
                self._receive_type(second, SERVER_MATCH_FOUND)
                self.assertEqual(
                    first_found["payload"]["time_control"]["mode"],
                    "fischer",
                )
                first_state = parse_game_state(
                    self._receive_type(first, SERVER_GAME_STATE)["payload"]
                )
                second_state = parse_game_state(
                    self._receive_type(second, SERVER_GAME_STATE)["payload"]
                )
                self.assertTrue(first_state.time_control.is_fischer)
                self.assertEqual(
                    first_state.time_control.main_seconds, 300
                )
                self.assertIsNone(first_state.move_timeout_seconds)
                self.assertIsNotNone(first_state.turn_deadline_ms)
                self.assertGreaterEqual(
                    first_state.black_remaining_ms or 0, 295_000
                )
                self.assertLessEqual(
                    first_state.black_remaining_ms or 0, 300_100
                )
                self.assertGreaterEqual(
                    first_state.white_remaining_ms or 0, 295_000
                )
                self.assertLessEqual(
                    first_state.white_remaining_ms or 0, 300_100
                )

                if first_state.your_color is second_state.your_color:
                    self.fail("双方执子颜色必须不同")
                black_ws = (
                    first
                    if first_state.your_color.value == "black"
                    else second
                )
                white_ws = second if black_ws is first else first
                black_ws.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 7},
                        request_id="fischer-move-1",
                    )
                )
                black_after = parse_game_state(
                    self._receive_type(black_ws, SERVER_GAME_STATE)[
                        "payload"
                    ]
                )
                white_after = parse_game_state(
                    self._receive_type(white_ws, SERVER_GAME_STATE)[
                        "payload"
                    ]
                )
                self.assertEqual(black_after.move_no, 1)
                self.assertEqual(white_after.move_no, 1)
                # 黑方落子后获得 +3 秒，余量不会低于初始 300 秒；
                # 白方尚未行动，余量保持初始值。
                self.assertGreaterEqual(
                    black_after.black_remaining_ms or 0, 300_000
                )
                self.assertGreaterEqual(
                    white_after.white_remaining_ms or 0, 299_000
                )
                self.assertLessEqual(
                    white_after.white_remaining_ms or 0, 300_000
                )
                self.assertIs(white_after.current_player.value, "white")
                self.assertGreaterEqual(
                    white_after.turn_deadline_ms or 0,
                    white_after.server_now_ms or 0,
                )
                self.assertEqual(
                    black_after.black_remaining_ms,
                    white_after.black_remaining_ms,
                )

    def test_different_time_controls_do_not_match(self) -> None:
        first_token, second_token, _ = self._match_fischer()
        with self.client.websocket_connect("/api/v1/ws") as first:
            with self.client.websocket_connect("/api/v1/ws") as second:
                self._authenticate(first, first_token)
                self._authenticate(second, second_token)
                first.send_json(
                    make_message(
                        CLIENT_QUICK_MATCH,
                        {
                            "time_control": {
                                "mode": "per_move",
                                "turn_seconds": 30,
                            }
                        },
                    )
                )
                self.assertEqual(
                    first.receive_json()["payload"]["state"], "queued"
                )
                second.send_json(
                    make_message(
                        CLIENT_QUICK_MATCH,
                        {
                            "time_control": {
                                "mode": "fischer",
                                "main_seconds": 300,
                                "increment_seconds": 3,
                            }
                        },
                    )
                )
                queued = second.receive_json()
                self.assertEqual(queued["type"], SERVER_QUEUE_STATUS)
                self.assertEqual(queued["payload"]["state"], "queued")
                for ws in (first, second):
                    ws.send_json(make_message(CLIENT_CANCEL_MATCH, {}))
                    idle = self._receive_type(ws, SERVER_QUEUE_STATUS)
                    self.assertEqual(idle["payload"]["state"], "idle")

    def test_invalid_time_control_is_rejected(self) -> None:
        token = self._register_access("fischer_invalid")
        with self.client.websocket_connect("/api/v1/ws") as ws:
            self._authenticate(ws, token)
            ws.send_json(
                make_message(
                    CLIENT_QUICK_MATCH,
                    {
                        "time_control": {
                            "mode": "fischer",
                            "main_seconds": 10,
                            "increment_seconds": 3,
                        }
                    },
                )
            )
            error = ws.receive_json()
            self.assertEqual(error["type"], SERVER_ERROR)
            self.assertIn("总时长", error["payload"]["reason"])

            ws.send_json(
                make_message(
                    CLIENT_CREATE_ROOM,
                    {
                        "time_control": {
                            "mode": "fischer",
                            "main_seconds": 300,
                            "increment_seconds": 999,
                        }
                    },
                )
            )
            error = ws.receive_json()
            self.assertEqual(error["type"], SERVER_ERROR)
            self.assertIn("加秒", error["payload"]["reason"])

    def test_per_move_legacy_field_still_works(self) -> None:
        token = self._register_access("legacy_seconds")
        with self.client.websocket_connect("/api/v1/ws") as ws:
            self._authenticate(ws, token)
            ws.send_json(
                make_message(CLIENT_QUICK_MATCH, {"turn_seconds": 45})
            )
            queued = ws.receive_json()
            self.assertEqual(queued["payload"]["state"], "queued")
            ws.send_json(make_message(CLIENT_CANCEL_MATCH, {}))
            ws.receive_json()


class ClockMathTests(ServerTestBase):
    def test_consume_fischer_clock_and_increment(self) -> None:
        async def scenario() -> None:
            manager = self.app.state.room_manager
            room = Room(
                code="CLOCK1",
                game_id="game-clock",
                black_user_id="black-id",
                white_user_id="white-id",
                black_name="B",
                white_name="W",
                time_control=TimeControl.fischer(300, 3),
                status="playing",
                connections={},
            )
            manager._reset_clocks_locked(room)
            with patch(
                "server.app.ws.rooms._now_ms", return_value=1_000
            ):
                manager._start_timeout_locked(room)
            self.assertEqual(room.clock_remaining_ms["black-id"], 300_000)
            self.assertEqual(room.turn_deadline_ms, 301_000)
            manager._cancel_timeout_locked(room)

            manager._consume_clock_locked(
                room, "black-id", at_ms=61_000
            )
            # 60 秒本手耗时 + 3 秒加秒。
            self.assertEqual(room.clock_remaining_ms["black-id"], 243_000)
            self.assertEqual(room.clock_remaining_ms["white-id"], 300_000)
            self.assertIsNone(room.clock_started_ms)

        asyncio.run(scenario())

    def test_per_move_resets_each_turn(self) -> None:
        async def scenario() -> None:
            manager = self.app.state.room_manager
            room = Room(
                code="CLOCK2",
                game_id="game-clock-2",
                black_user_id="black-id",
                white_user_id="white-id",
                black_name="B",
                white_name="W",
                time_control=TimeControl.per_move(30),
                status="playing",
                connections={},
            )
            manager._reset_clocks_locked(room)
            with patch(
                "server.app.ws.rooms._now_ms", return_value=1_000
            ):
                manager._start_timeout_locked(room)
            self.assertEqual(
                room.clock_remaining_ms["black-id"], 30_000
            )
            manager._cancel_timeout_locked(room)
            manager._consume_clock_locked(
                room, "black-id", at_ms=20_000
            )
            # 模拟黑方落子后轮到白方。
            room.game.place_stone(7, 7)
            with patch(
                "server.app.ws.rooms._now_ms", return_value=20_000
            ):
                manager._start_timeout_locked(room)
            self.assertEqual(
                room.clock_remaining_ms["white-id"], 30_000
            )
            self.assertEqual(room.turn_deadline_ms, 50_000)
            manager._cancel_timeout_locked(room)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
