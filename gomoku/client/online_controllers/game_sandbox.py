"""联机对局的本地沙盘：打开、关闭与按钮状态同步。

沙盘完全在本地运行，不会向服务器发送任何落子请求；真实局面推进时
由同步模块把新棋盘交给沙盘作为基线。
"""

from __future__ import annotations

from typing import Optional

from gomoku.client.online_controllers.common import PanelController
from gomoku.client.sessions.online import OnlineGameSession


class GameSandboxController(PanelController):
    """对局页沙盘面板的显隐与可用状态。"""

    def on_sandbox_toggled(self) -> None:
        """「沙盘推演」按钮：打开或关闭沙盘。"""
        if self.panel._sandbox_open:
            self.panel._close_sandbox()
        else:
            self.panel._open_sandbox()

    def open_sandbox(self) -> bool:
        """打开沙盘：载入当前对局局面，并遮挡真实棋盘。

        沙盘完全在本地运行，不会向服务器发送任何落子请求。
        """
        session = self.panel._session
        if session is None or session.snapshot is None:
            # 还没有对局（或对局尚未开始），没有可推演的局面。
            return False
        self.panel.sandbox_panel.load_position(session.view)
        self.panel.sandbox_panel.setVisible(True)
        self.panel._sandbox_open = True
        self.panel.sandbox_button.setText("退出沙盘")
        # 真实棋盘被沙盘盖住，不能再接受点击。
        self.panel.board_widget.set_interaction_enabled(False)
        return True

    def close_sandbox(self) -> None:
        """关闭沙盘：恢复真实棋盘。"""
        self.panel._hide_sandbox()
        # 整页刷新：真实棋盘的交互状态按会话重新计算。
        self.panel._sync_game_page()

    def hide_sandbox(self) -> None:
        """隐藏沙盘并复位沙盘状态，但不触发整页刷新。"""
        self.panel.sandbox_panel.setVisible(False)
        self.panel._sandbox_open = False
        self.panel.sandbox_button.setText("沙盘推演")

    def sync_sandbox_button(
        self, session: Optional[OnlineGameSession]
    ) -> None:
        """同步沙盘按钮的可用状态：没有对局时不可用。"""
        has_game = session is not None and session.snapshot is not None
        # 沙盘已经打开时按钮必须保持可用，否则用户会被困在沙盘里
        # 无法退出（例如中途断线导致会话消失）。
        self.panel.sandbox_button.setEnabled(
            (has_game and self.panel._ws_connected) or self.panel._sandbox_open
        )
