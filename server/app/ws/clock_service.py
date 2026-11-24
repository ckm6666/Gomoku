"""回合计时、掉线宽限与悔棋过期定时器。

所有 ``asyncio.Task`` 的创建/取消与余量计算集中在这里。定时器到期后
需要终局或广播时，通过构造时注入的 ``ClockHooks``（由 ``RoomManager``
实现）回调，避免服务反向导入 ``rooms.py`` 造成循环依赖。

当前时间的来源由 ``now_ms`` 回调注入：``rooms.py`` 传入
``lambda: _now_ms()``，因此测试对 ``server.app.ws.rooms._now_ms`` 的
monkeypatch 仍然生效。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Protocol

from gomoku.shared.rules import Player
from server.app.config import Settings
from server.app.ws.entities import Room

logger = logging.getLogger(__name__)

#: 悔棋请求等待对手回应的最长时间；超时自动取消并恢复回合计时。
UNDO_REQUEST_TTL_SECONDS = 30.0


class ClockHooks(Protocol):
    """计时器到期时需要门面执行的副作用。"""

    async def _broadcast_undo_status(
        self,
        room: Room,
        state: str,
        requester_id: Optional[str],
        reason: str = "",
    ) -> None: ...

    async def _broadcast_game_state(self, room: Room) -> None: ...

    async def _end_game_locked(
        self, room: Room, winner_user_id: str, reason: str
    ) -> None: ...


class ClockService:
    """房间时钟与超时/逃跑/悔棋过期任务的管理者。"""

    def __init__(
        self,
        settings: Settings,
        now_ms: Callable[[], int],
        hooks: ClockHooks,
    ) -> None:
        self._settings = settings
        self._now_ms = now_ms
        self._hooks = hooks

    # ------------------------------------------------------------------
    # 悔棋过期
    # ------------------------------------------------------------------
    def start_undo_expiry_locked(self, room: Room) -> None:
        """悔棋请求在无人回应时自动过期，避免回合永久暂停。"""
        self.cancel_undo_timer_locked(room)
        room.undo_token += 1
        token = room.undo_token
        room.undo_task = asyncio.create_task(
            self._undo_expire_after(room, token)
        )

    def cancel_undo_timer_locked(self, room: Room) -> None:
        task = room.undo_task
        room.undo_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def clear_undo_locked(self, room: Room) -> None:
        room.undo_requester_id = None
        self.cancel_undo_timer_locked(room)

    async def _undo_expire_after(self, room: Room, token: int) -> None:
        try:
            await asyncio.sleep(UNDO_REQUEST_TTL_SECONDS)
            async with room.lock:
                if (
                    room.undo_token != token
                    or room.undo_requester_id is None
                ):
                    return
                requester_id = room.undo_requester_id
                self.clear_undo_locked(room)
                room.state_version += 1
                if room.status == "playing":
                    self.start_timeout_locked(room)
                await self._hooks._broadcast_undo_status(
                    room, "expired", requester_id, "悔棋请求已超时"
                )
                await self._hooks._broadcast_game_state(room)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # 掉线宽限
    # ------------------------------------------------------------------
    def start_escape_timer_locked(
        self, room: Room, user_id: str
    ) -> None:
        """掉线宽限计时：到期仍未重连则由对手获胜。"""
        self.cancel_escape_timer_locked(room, user_id)
        room.escape_tokens[user_id] = room.escape_tokens.get(user_id, 0) + 1
        token = room.escape_tokens[user_id]
        seconds = max(0.05, float(self._settings.disconnect_forfeit_seconds))
        room.escape_tasks[user_id] = asyncio.create_task(
            self._escape_after(room, user_id, token, seconds)
        )

    def cancel_escape_timer_locked(
        self, room: Room, user_id: str
    ) -> None:
        task = room.escape_tasks.pop(user_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def cancel_all_escape_timers_locked(self, room: Room) -> None:
        for task in list(room.escape_tasks.values()):
            if task is not asyncio.current_task():
                task.cancel()
        room.escape_tasks.clear()

    async def _escape_after(
        self,
        room: Room,
        user_id: str,
        token: int,
        seconds: float,
    ) -> None:
        try:
            await asyncio.sleep(seconds)
            async with room.lock:
                if room.escape_tokens.get(user_id) != token:
                    return
                room.escape_tasks.pop(user_id, None)
                if room.status != "playing":
                    return
                if user_id in room.connections:
                    return
                winner_id = room.opponent_id(user_id)
                if winner_id is None:
                    return
                await self._hooks._end_game_locked(
                    room, winner_id, "escape"
                )
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # 回合计时
    # ------------------------------------------------------------------
    def current_clock_user_id(self, room: Room) -> Optional[str]:
        if room.game.current_player is Player.BLACK:
            return room.black_user_id
        if room.game.current_player is Player.WHITE:
            return room.white_user_id
        return None

    def cancel_timeout_locked(self, room: Room) -> None:
        task = room.timeout_task
        room.timeout_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def reset_clocks_locked(self, room: Room) -> None:
        """新对局/再战开始时重置双方余量。"""
        initial = room.time_control.initial_remaining_ms()
        room.clock_remaining_ms = {
            user_id: initial for user_id in room.participant_ids()
        }
        room.clock_started_ms = None
        room.turn_deadline_ms = None

    def pause_clock_locked(self, room: Room) -> None:
        """暂停计时（悔棋等待期间），把已用时间从当前余量中扣除。"""
        if room.clock_started_ms is not None:
            current_id = self.current_clock_user_id(room)
            if (
                current_id is not None
                and room.turn_deadline_ms is not None
            ):
                room.clock_remaining_ms[current_id] = max(
                    0, room.turn_deadline_ms - self._now_ms()
                )
        self.cancel_timeout_locked(room)
        room.clock_started_ms = None
        room.turn_deadline_ms = None

    def consume_clock_locked(
        self, room: Room, user_id: str, at_ms: Optional[int] = None
    ) -> None:
        """落子成功后结算走子方本手耗时。

        per_move 模式下一手重新给满；Fischer 模式从余量里扣除本手耗时，
        再加上固定加秒。调用方持有 ``room.lock``。
        """
        control = room.time_control
        if control.is_fischer:
            now = self._now_ms() if at_ms is None else at_ms
            started = room.clock_started_ms
            remaining = room.clock_remaining_ms.get(
                user_id, control.initial_remaining_ms()
            )
            if started is not None:
                remaining = max(0, remaining - max(0, now - started))
            room.clock_remaining_ms[user_id] = (
                remaining + control.increment_seconds * 1000
            )
        else:
            room.clock_remaining_ms[user_id] = 0
        room.clock_started_ms = None
        room.turn_deadline_ms = None

    def snapshot_remaining_locked(
        self, room: Room, user_id: str
    ) -> Optional[int]:
        """快照里展示的余量；每手模式不使用双方余量字段。"""
        if not room.time_control.is_fischer:
            return None
        remaining = room.clock_remaining_ms.get(
            user_id, room.time_control.initial_remaining_ms()
        )
        if (
            room.clock_started_ms is not None
            and room.turn_deadline_ms is not None
            and self.current_clock_user_id(room) == user_id
        ):
            remaining = max(0, room.turn_deadline_ms - self._now_ms())
        return remaining

    def start_timeout_locked(self, room: Room) -> None:
        self.cancel_timeout_locked(room)
        if room.status != "playing":
            room.turn_deadline_ms = None
            room.clock_started_ms = None
            return
        now = self._now_ms()
        current_id = self.current_clock_user_id(room)
        if current_id is None:
            room.turn_deadline_ms = None
            room.clock_started_ms = None
            return
        control = room.time_control
        if room.clock_started_ms is None:
            room.clock_started_ms = now
            if not control.is_fischer:
                # 每手模式：每个回合都重新给满。
                room.clock_remaining_ms[current_id] = (
                    control.turn_seconds * 1000
                )
            else:
                room.clock_remaining_ms.setdefault(
                    current_id, control.initial_remaining_ms()
                )
        remaining = max(
            0,
            room.clock_remaining_ms.get(
                current_id, control.initial_remaining_ms()
            ),
        )
        room.turn_deadline_ms = room.clock_started_ms + remaining
        room.turn_token += 1
        token = room.turn_token
        delay = max(0.001, (room.turn_deadline_ms - now) / 1000.0)
        room.timeout_task = asyncio.create_task(
            self._timeout_after(room, token, delay)
        )

    def timeout_reason_locked(self, room: Room, loser_id: str) -> str:
        """超时判负的原因：掉线方按“逃跑”，在线方按普通“超时”。

        落子路径的补判和计时任务路径必须给出同一个结果，否则同一场景
        会因为哪条代码先跑到而留下不同的 ``result_reason``，统计、举报
        审计和客户端文案都会跟着漂移。
        """
        return "escape" if loser_id not in room.connections else "timeout"

    async def _timeout_after(
        self, room: Room, token: int, delay_seconds: float
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            async with room.lock:
                if room.status != "playing" or room.turn_token != token:
                    return
                loser_id = self.current_clock_user_id(room)
                if loser_id is None:
                    return
                winner_id = room.opponent_id(loser_id)
                if winner_id is None:
                    return
                # 掉线且未在宽限期内回来，按“逃跑”而不是普通超时记录。
                reason = self.timeout_reason_locked(room, loser_id)
                await self._hooks._end_game_locked(
                    room, winner_id, reason
                )
        except asyncio.CancelledError:
            return


__all__ = ["ClockHooks", "ClockService", "UNDO_REQUEST_TTL_SECONDS"]
