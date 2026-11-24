"""ClockService 的单元测试：余量计算、超时/逃跑/悔棋定时器。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from gomoku.shared.time_control import TimeControl
from server.app.ws.clock_service import ClockService
from server.app.ws.entities import Room


class _Hooks:
    def __init__(self) -> None:
        self.ended: list[tuple[str, str, str]] = []
        self.undo_status: list[tuple[str, str, str]] = []
        self.game_states: list[str] = []

    async def _broadcast_undo_status(
        self, room, state, requester_id, reason=""
    ) -> None:
        self.undo_status.append((state, requester_id or "", reason))

    async def _broadcast_game_state(self, room) -> None:
        self.game_states.append(room.game_id)

    async def _end_game_locked(self, room, winner_user_id, reason) -> None:
        self.ended.append((room.game_id, winner_user_id, reason))


def _settings(**overrides):
    values = {"disconnect_forfeit_seconds": 5.0}
    values.update(overrides)
    return SimpleNamespace(**values)


def _room(control: TimeControl, status: str = "playing") -> Room:
    return Room(
        code="CLOCK",
        game_id="game-clock",
        black_user_id="black-id",
        white_user_id="white-id",
        black_name="B",
        white_name="W",
        time_control=control,
        status=status,
        connections={},
    )


class ClockMathTests(unittest.TestCase):
    def test_fischer_consumes_elapsed_and_increments(self) -> None:
        async def scenario() -> None:
            now = [1_000]
            clocks = ClockService(
                _settings(), lambda: now[0], _Hooks()
            )
            room = _room(TimeControl.fischer(300, 3))
            clocks.reset_clocks_locked(room)
            clocks.start_timeout_locked(room)
            self.assertEqual(room.clock_remaining_ms["black-id"], 300_000)
            self.assertEqual(room.turn_deadline_ms, 301_000)
            clocks.cancel_timeout_locked(room)

            clocks.consume_clock_locked(
                room, "black-id", at_ms=61_000
            )
            # 60 秒本手耗时 + 3 秒加秒。
            self.assertEqual(room.clock_remaining_ms["black-id"], 243_000)
            self.assertEqual(room.clock_remaining_ms["white-id"], 300_000)
            self.assertIsNone(room.clock_started_ms)

        asyncio.run(scenario())

    def test_per_move_resets_each_turn(self) -> None:
        async def scenario() -> None:
            now = [1_000]
            clocks = ClockService(
                _settings(), lambda: now[0], _Hooks()
            )
            room = _room(TimeControl.per_move(30))
            clocks.reset_clocks_locked(room)
            clocks.start_timeout_locked(room)
            self.assertEqual(room.clock_remaining_ms["black-id"], 30_000)
            clocks.cancel_timeout_locked(room)
            clocks.consume_clock_locked(
                room, "black-id", at_ms=20_000
            )
            # 模拟黑方落子后轮到白方。
            room.game.place_stone(7, 7)
            now[0] = 20_000
            clocks.start_timeout_locked(room)
            self.assertEqual(room.clock_remaining_ms["white-id"], 30_000)
            self.assertEqual(room.turn_deadline_ms, 50_000)
            clocks.cancel_timeout_locked(room)

        asyncio.run(scenario())

    def test_pause_commits_elapsed_time_and_stops_timer(self) -> None:
        async def scenario() -> None:
            now = [1_000]
            clocks = ClockService(
                _settings(), lambda: now[0], _Hooks()
            )
            room = _room(TimeControl.fischer(300, 3))
            clocks.reset_clocks_locked(room)
            clocks.start_timeout_locked(room)
            now[0] = 11_000
            clocks.pause_clock_locked(room)
            self.assertEqual(
                room.clock_remaining_ms["black-id"], 290_000
            )
            self.assertIsNone(room.turn_deadline_ms)
            self.assertIsNone(room.clock_started_ms)
            self.assertIsNone(room.timeout_task)

        asyncio.run(scenario())

    def test_snapshot_remaining_uses_deadline_only_for_current(self) -> None:
        async def scenario() -> None:
            now = [1_000]
            clocks = ClockService(
                _settings(), lambda: now[0], _Hooks()
            )
            room = _room(TimeControl.fischer(300, 3))
            clocks.reset_clocks_locked(room)
            clocks.start_timeout_locked(room)
            now[0] = 61_000
            self.assertEqual(
                clocks.snapshot_remaining_locked(room, "black-id"),
                240_000,
            )
            self.assertEqual(
                clocks.snapshot_remaining_locked(room, "white-id"),
                300_000,
            )
            clocks.cancel_timeout_locked(room)
            # 每手模式不展示双方余量。
            self.assertIsNone(
                clocks.snapshot_remaining_locked(
                    _room(TimeControl.per_move(30)), "black-id"
                )
            )

        asyncio.run(scenario())

    def test_current_clock_user_id(self) -> None:
        clocks = ClockService(_settings(), lambda: 0, _Hooks())
        room = _room(TimeControl.per_move(30))
        self.assertEqual(
            clocks.current_clock_user_id(room), "black-id"
        )
        room.game.place_stone(7, 7)
        self.assertEqual(
            clocks.current_clock_user_id(room), "white-id"
        )


class TimeoutReasonTests(unittest.TestCase):
    def test_disconnected_loser_is_escape(self) -> None:
        clocks = ClockService(_settings(), lambda: 0, _Hooks())
        room = _room(TimeControl.per_move(30))
        room.connections["black-id"] = SimpleNamespace(user_id="black-id")
        self.assertEqual(
            clocks.timeout_reason_locked(room, "black-id"), "timeout"
        )
        self.assertEqual(
            clocks.timeout_reason_locked(room, "white-id"), "escape"
        )


class TimerLifecycleTests(unittest.TestCase):
    def test_undo_expiry_timer_is_replaced_and_cleared(self) -> None:
        async def scenario() -> None:
            clocks = ClockService(_settings(), lambda: 0, _Hooks())
            room = _room(TimeControl.per_move(30))
            room.undo_requester_id = "black-id"
            clocks.start_undo_expiry_locked(room)
            first_task = room.undo_task
            self.assertIsNotNone(first_task)
            self.assertEqual(room.undo_token, 1)

            clocks.start_undo_expiry_locked(room)
            self.assertEqual(room.undo_token, 2)
            self.assertIsNot(room.undo_task, first_task)
            await asyncio.sleep(0)
            self.assertTrue(first_task.cancelled() or first_task.done())

            clocks.clear_undo_locked(room)
            self.assertIsNone(room.undo_requester_id)
            self.assertIsNone(room.undo_task)

        asyncio.run(scenario())

    def test_escape_timer_tracks_tokens_and_cancels(self) -> None:
        async def scenario() -> None:
            clocks = ClockService(_settings(), lambda: 0, _Hooks())
            room = _room(TimeControl.per_move(30))
            clocks.start_escape_timer_locked(room, "black-id")
            self.assertIn("black-id", room.escape_tasks)
            self.assertEqual(room.escape_tokens["black-id"], 1)
            clocks.start_escape_timer_locked(room, "black-id")
            self.assertEqual(room.escape_tokens["black-id"], 2)
            clocks.cancel_escape_timer_locked(room, "black-id")
            self.assertNotIn("black-id", room.escape_tasks)
            clocks.start_escape_timer_locked(room, "white-id")
            clocks.cancel_all_escape_timers_locked(room)
            self.assertEqual(room.escape_tasks, {})

        asyncio.run(scenario())

    def test_timeout_callback_ends_game_with_shared_reason(self) -> None:
        async def scenario() -> None:
            hooks = _Hooks()
            clocks = ClockService(_settings(), lambda: 1_000, hooks)
            room = _room(TimeControl.fischer(60, 0))
            room.connections = {
                "black-id": SimpleNamespace(user_id="black-id"),
                "white-id": SimpleNamespace(user_id="white-id"),
            }
            clocks.reset_clocks_locked(room)
            # 把黑方余量压到 1ms，让超时任务几乎立即到期。
            room.clock_remaining_ms["black-id"] = 1
            clocks.start_timeout_locked(room)
            await asyncio.sleep(0.05)
            self.assertEqual(
                hooks.ended, [("game-clock", "white-id", "timeout")]
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
