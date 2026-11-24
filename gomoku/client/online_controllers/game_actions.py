"""联机对局的操作：落子、悔棋协商、认输与再战。

这些方法把用户在按钮/棋盘上的操作翻译成会话调用；按钮文案、可用
状态与提示标签的刷新也在这里，保持与拆分前一致。
"""

from __future__ import annotations

from gomoku.client.online_controllers.common import (
    PanelController,
    host_module,
)


class GameActionsController(PanelController):
    """落子、悔棋、认输与再战操作。"""

    def on_board_clicked(self, row: int, col: int) -> None:
        """棋盘落子：只有会话接受人类输入时才请求落子。"""
        if self.panel._session is not None and self.panel._session.accepts_human_input:
            self.panel._session.request_move(row, col)

    def on_undo_clicked(self) -> None:
        """正常时发起悔棋请求；对手请求待回应时，此按钮表示同意。"""
        if self.panel._session is None:
            return
        if self.panel._session.undo_pending:
            if not self.panel._session.undo_requested_by_you:
                if self.panel._undo_responded:
                    # 已经回应过：等服务器广播新状态，不要重复发送。
                    return
                self.panel._undo_responded = True
                self.panel._session.respond_undo(True)
                self.panel._set_undo_text("等待服务器确认")
                self.panel.undo_button.setEnabled(False)
                self.panel._show_undo_notice("已同意悔棋，等待服务器确认…")
            return
        self.panel._session.request_undo()

    def on_undo_declined(self) -> None:
        """拒绝对手的悔棋请求。"""
        if self.panel._session is not None and self.panel._session.undo_pending:
            self.panel._undo_responded = True
            self.panel._session.respond_undo(False)
            self.panel._set_undo_text("等待服务器确认")
            self.panel.undo_button.setEnabled(False)

    def on_undo_request_received(self, name: str) -> None:
        """收到对手的悔棋请求。"""
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        if self.panel._session is not None and self.panel._session.is_spectator:
            # 观战者不参与悔棋协商。
            return
        # 新的悔棋请求：允许重新回应。
        self.panel._undo_responded = False
        self.panel._show_undo_notice(f"{name or '对手'} 请求悔棋，是否同意？")
        self.panel._sync_game_page()

    def on_undo_status_changed(self, state: str, reason: str) -> None:
        """服务器广播悔棋状态变化。"""
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        if state == "requested":
            if (
                self.panel._session is not None
                and self.panel._session.undo_requested_by_you
            ):
                self.panel._show_undo_notice("已发送悔棋请求，等待对手回应…")
        elif state == "accepted":
            self.panel._show_undo_notice(reason or "悔棋成功")
        elif state == "declined":
            self.panel._show_undo_notice(reason or "对手拒绝了悔棋请求")
        elif state == "expired":
            self.panel._show_undo_notice(reason or "悔棋请求已超时")
        elif state == "cancelled":
            self.panel._show_undo_notice(reason or "悔棋请求已取消")
        if state != "requested":
            # 请求已经有结果（同意/拒绝/超时/取消）：按钮恢复可用。
            self.panel._undo_responded = False
        self.panel._sync_game_page()

    def show_undo_notice(self, text: str) -> None:
        """在悔棋提示标签上显示一条消息。"""
        self.panel.undo_status_label.setText(text)
        self.panel.undo_status_label.setVisible(bool(text))
        if text:
            self.panel._undo_notice_timer.start(5000)

    def clear_undo_notice(self) -> None:
        """清空悔棋提示标签。"""
        self.panel.undo_status_label.setText("")
        self.panel.undo_status_label.setVisible(False)

    def set_undo_text(self, text: str) -> None:
        """更新悔棋按钮文案（避免无谓的重绘）。"""
        if self.panel.undo_button.text() != text:
            self.panel.undo_button.setText(text)

    def refresh_undo_controls(self) -> None:
        """刷新悔棋按钮的次数/冷却/待回应状态。"""
        session = self.panel._session
        if session is None:
            return
        if session.undo_pending:
            if session.undo_requested_by_you:
                self.panel._set_undo_text("等待对方同意")
                enabled = False
            elif self.panel._undo_responded:
                # 已回应、服务器还没广播结果：先按住按钮，别重复发送。
                self.panel._set_undo_text("等待服务器确认")
                enabled = False
            else:
                self.panel._set_undo_text("同意悔棋")
                enabled = self.panel._ws_connected
            self.panel.undo_button.setEnabled(enabled)
            self.panel.undo_button.setToolTip("")
            return

        remaining = session.undo_remaining
        cooldown = session.undo_cooldown_remaining
        if session.undo_limit <= 0:
            self.panel._set_undo_text("本局未开启悔棋")
            enabled = False
        elif remaining <= 0:
            self.panel._set_undo_text("本局悔棋已用完")
            enabled = False
        elif cooldown > 0:
            self.panel._set_undo_text(f"悔棋冷却 {cooldown}s")
            enabled = False
        else:
            self.panel._set_undo_text("请求悔棋")
            enabled = self.panel._ws_connected and session.can_undo
        self.panel.undo_button.setEnabled(enabled)
        if session.undo_limit > 0:
            tooltip = (
                f"本局剩余 {remaining}/{session.undo_limit} 次；"
                f"每 {session.undo_cooldown_seconds} 秒最多请求一次"
            )
        else:
            tooltip = ""
        if self.panel.undo_button.toolTip() != tooltip:
            self.panel.undo_button.setToolTip(tooltip)

    def on_resign(self) -> None:
        """认输：二次确认后通知服务器。"""
        if self.panel._session is None or self.panel._closing:
            return
        snapshot = self.panel._session.snapshot
        if snapshot is None or snapshot.is_game_over:
            return
        dialog = host_module().ConfirmDialog(
            "确认认输",
            "认输后本局直接判负，确定要认输吗？",
            confirm_text="认输",
            cancel_text="继续对局",
            seal_text="负",
            parent=self.panel,
        )
        try:
            dialog.exec()
            confirmed = dialog.confirm_requested
        finally:
            dialog.deleteLater()
        if not confirmed or self.panel._session is None:
            return
        snapshot = self.panel._session.snapshot
        if snapshot is not None and not snapshot.is_game_over:
            self.panel._session.resign()

    def on_rematch(self) -> None:
        """再战：排位赛离开房间重新排队，休闲对局直接请求。"""
        if self.panel._session is None:
            return
        if self.panel._session.rated:
            # 排位赛不能与同一对手直接再战，离开房间后重新排队。
            self.panel._auto_requeue = True
            if not self.panel._session.leave_room():
                self.panel._auto_requeue = False
                self.panel._set_game_error(
                    "无法离开排位房间，请稍后重试"
                )
            return
        self.panel._session.request_rematch()
