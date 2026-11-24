"""联机对局的时钟与倒计时。

倒计时完全依据服务器快照里的截止时间计算，客户端不自行判定超时
胜负。这里的方法由 :class:`~gomoku.client.online_controllers.game.GameController`
门面转发，行为与拆分前一致。
"""

from __future__ import annotations

import time

from gomoku.client.online_controllers.common import (
    PanelController,
    format_clock_ms,
    host_module,
)
from gomoku.shared.protocol import GameSnapshot
from gomoku.shared.rules import Player


class GameClockController(PanelController):
    """对局页的时钟标签、大号倒计时与 Fischer 余量显示。"""

    def apply_snapshot_deadline(self, snapshot: GameSnapshot) -> None:
        """依据服务器时间计算本手倒计时，不在客户端自行判定超时胜负。"""
        self.panel._snapshot_monotonic = time.monotonic()
        if (
            not snapshot.is_game_over
            and snapshot.turn_deadline_ms is not None
            and snapshot.server_now_ms is not None
        ):
            seconds = (
                snapshot.turn_deadline_ms - snapshot.server_now_ms
            ) / 1000.0
            self.panel._deadline_monotonic = time.monotonic() + max(0.0, seconds)
            # 同一手的截止时间与手数在服务器上是固定的：只有它们变了才
            # 说明进入了新的一手，提示音的节拍要重新开始。
            turn_key = (snapshot.turn_deadline_ms, snapshot.move_no)
            if turn_key != self.panel._countdown_turn_key:
                self.panel._countdown_turn_key = turn_key
                self.panel._countdown_beep_second = None
        else:
            self.panel._deadline_monotonic = None
            self.panel._countdown_turn_key = None
            self.panel._countdown_beep_second = None

    def update_clock_label(self) -> None:
        """按当前快照刷新时钟标签，并在需要时显示大号倒计时。"""
        # 聊天控件状态每次心跳都刷新一次（即使当前没有对局）。
        self.panel._refresh_chat_controls()
        if (
            self.panel._session is None
            or self.panel._session.snapshot is None
            or self.panel.stack.currentWidget() is not self.panel.game_page
        ):
            self.panel._hide_countdown()
            return
        snapshot = self.panel._session.snapshot
        if not self.panel._session.is_spectator:
            self.panel._refresh_undo_controls()
        if snapshot.is_game_over:
            self.panel.clock_label.setText("对局结束")
            self.panel._hide_countdown()
            return
        if not self.panel._ws_connected:
            self.panel.clock_label.setText("连接中断")
            self.panel._hide_countdown()
            return
        if self.panel._session.undo_pending and not self.panel._session.is_spectator:
            self.panel.clock_label.setText(
                "等待对手回应悔棋"
                if self.panel._session.undo_requested_by_you
                else "请回应悔棋请求"
            )
            self.panel._hide_countdown()
            return
        time_control = snapshot.time_control
        if time_control is not None and time_control.is_fischer:
            self.panel.clock_label.setText(self.panel._fischer_clock_text(snapshot))
            self.panel._hide_countdown()
            return
        if self.panel._deadline_monotonic is None:
            self.panel.clock_label.setText("等待服务器…")
            self.panel._hide_countdown()
            return
        remaining = self.panel._deadline_monotonic - time.monotonic()
        if remaining <= 0:
            self.panel.clock_label.setText("本手时间已到，等待服务器判定…")
            self.panel._hide_countdown()
            return
        self.panel.clock_label.setText("本手倒计时")
        self.panel._show_countdown(int(remaining + 0.999), snapshot)

    def hide_countdown(self) -> None:
        """没有可显示的倒计时：收起大号秒数。"""
        label = getattr(self.panel, "countdown_label", None)
        if label is None:
            return
        label.setVisible(False)
        self.panel._set_countdown_urgent(False)

    def set_countdown_urgent(self, urgent: bool) -> None:
        """切换倒计时的“最后 10 秒”红色状态。"""
        label = getattr(self.panel, "countdown_label", None)
        if label is None:
            return
        value = "true" if urgent else "false"
        if label.property("urgent") == value:
            return
        label.setProperty("urgent", value)
        label.style().unpolish(label)
        label.style().polish(label)
        label.update()

    def show_countdown(self, seconds: int, snapshot: GameSnapshot) -> None:
        """显示本手倒计时，并在最后几秒逐秒提示。"""
        label = getattr(self.panel, "countdown_label", None)
        if label is None:
            return
        seconds = max(0, int(seconds))
        label.setText(str(seconds))
        self.panel._set_countdown_urgent(
            seconds <= host_module().COUNTDOWN_URGENT_SECONDS
        )
        label.setVisible(True)
        self.panel._maybe_beep_countdown(seconds, snapshot)

    def maybe_beep_countdown(
        self, seconds: int, snapshot: GameSnapshot
    ) -> None:
        """最后 10 秒逐秒响一次“咚”。

        只在自己的回合响：等对手落子时响提示音会让人误以为是自己要
        超时了。同一秒只响一次；进入新的一手后重新开始计数。
        """
        if seconds > host_module().COUNTDOWN_URGENT_SECONDS:
            return
        if not self.panel._countdown_is_mine(snapshot):
            return
        if self.panel._countdown_beep_second == seconds:
            return
        self.panel._countdown_beep_second = seconds
        host_module().get_audio_manager().play_tick()

    def countdown_is_mine(self, snapshot: GameSnapshot) -> bool:
        """当前这一手是否轮到自己（观战者不算）。"""
        session = self.panel._session
        if session is None or session.is_spectator:
            return False
        return snapshot.current_player is snapshot.your_color

    def fischer_clock_text(self, snapshot: GameSnapshot) -> str:
        """Fischer 双方余量：只减去走子方从快照以来的本地耗时。"""
        black = snapshot.black_remaining_ms or 0
        white = snapshot.white_remaining_ms or 0
        if (
            self.panel._snapshot_monotonic is not None
            and snapshot.turn_deadline_ms is not None
        ):
            elapsed_ms = int(
                max(0.0, time.monotonic() - self.panel._snapshot_monotonic)
                * 1000
            )
            if snapshot.current_player is Player.BLACK:
                black = max(0, black - elapsed_ms)
            else:
                white = max(0, white - elapsed_ms)
        if self.panel._session is not None and self.panel._session.is_spectator:
            return f"黑 {format_clock_ms(black)} · 白 {format_clock_ms(white)}"
        your_color = (
            self.panel._session.snapshot.your_color
            if self.panel._session is not None
            and self.panel._session.snapshot is not None
            else None
        )
        if your_color is Player.WHITE:
            yours, opponent = white, black
        else:
            yours, opponent = black, white
        return (
            f"你 {format_clock_ms(yours)} · "
            f"对手 {format_clock_ms(opponent)}"
        )
