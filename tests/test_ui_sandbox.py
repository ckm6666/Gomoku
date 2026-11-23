"""沙盘推演（sandbox）面板的离屏 UI 测试。

沙盘面板叠在真实联机棋盘之上，用户在沙盘上自行执黑执白推演。覆盖三件事：

* 「沙盘推演」按钮能打开/关闭沙盘，并且打开时真实棋盘不可点击；
* **沙盘永远不与服务器通信**（最关键的隔离性保护）；
* 真实对局推进时沙盘会提示局面已更新。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.online_panel import OnlinePanel  # noqa: E402
from gomoku.client.sessions.online import OnlineGameSession  # noqa: E402
from gomoku.shared.rules import Player  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402
from tests.test_spectate import (  # noqa: E402
    authenticate,
    blank_board,
    state_message,
)

# 保持 QApplication 单例；若 UI 测试先创建则复用同一个实例。
QT_APP = QApplication.instance() or QApplication([])


def board_with_stones(
    stones: dict[tuple[int, int], str] | None = None,
) -> list[list[str | None]]:
    """构造一张带棋子的服务器棋盘（格子用 "black"/"white" 表示）。"""
    board = blank_board()
    for (row, col), color in (stones or {}).items():
        board[row][col] = color
    return board


def make_state_message(
    *,
    stones: dict[tuple[int, int], str] | None = None,
    your_color: str | None = "black",
    current: str = "white",
    move_no: int = 2,
) -> dict:
    """构造一个带棋子的符合协议的服务器局面消息。"""
    payload = dict(state_message(
        your_color=your_color,
        current=current,
        move_no=move_no,
    ))
    payload["payload"]["board"] = board_with_stones(stones)
    return payload


class SandboxUiTests(unittest.TestCase):
    """沙盘面板的离屏 UI 行为。"""

    def _panel(
        self,
        *,
        stones: dict[tuple[int, int], str] | None = None,
        your_color: str | None = "black",
        current: str = "black",
        move_no: int = 2,
        with_game: bool = True,
    ) -> tuple[OnlinePanel, OnlineGameSession, FakeTransport]:
        """搭一个联机对局页并返回（面板、会话、传输）。"""
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        if with_game:
            transport.receive(
                make_state_message(
                    stones=stones,
                    your_color=your_color,
                    current=current,
                    move_no=move_no,
                )
            )
        panel = OnlinePanel()
        panel._session = session
        panel._ws_connected = True
        # 真实运行时面板会订阅会话信号，这里补上同样的订阅关系。
        session.state_changed.connect(panel._on_online_state_changed)
        panel._show_game_page()
        panel._sync_game_page()
        # 让面板真正可见，否则子控件的 isVisible() 恒为 False。
        panel.show()
        QT_APP.processEvents()
        return panel, session, transport

    def _teardown(
        self,
        panel: OnlinePanel,
        session: OnlineGameSession,
        transport: FakeTransport,
    ) -> None:
        """收尾：先关面板，再关会话与传输。"""
        panel.close()
        session.shutdown()
        transport.close()
        QT_APP.processEvents()

    def test_sandbox_button_opens_and_covers_the_real_board(self) -> None:
        """打开沙盘后沙盘要盖住真实棋盘，真实棋盘不能再点。"""
        panel, session, transport = self._panel()
        try:
            self.assertFalse(panel._sandbox_open)
            self.assertTrue(panel.sandbox_button.isEnabled())
            # 打开之前真实棋盘是可点的，打开之后必须被禁用。
            self.assertTrue(panel.board_widget._interaction_enabled)

            panel.sandbox_button.click()

            self.assertTrue(panel._sandbox_open)
            self.assertTrue(panel.sandbox_panel.isVisible())
            self.assertTrue(panel.sandbox_panel.is_loaded)
            # 真实棋盘被覆盖，不能再接受点击。
            self.assertFalse(panel.board_widget._interaction_enabled)
            self.assertEqual(panel.sandbox_button.text(), "退出沙盘")
        finally:
            self._teardown(panel, session, transport)

    def test_sandbox_click_never_sends_a_move_to_the_server(self) -> None:
        """最重要的保护：沙盘里的落子绝不能发到服务器。"""
        panel, session, transport = self._panel()
        try:
            panel.sandbox_button.click()
            # 特意让真实棋盘处于“可落子”状态：万一沙盘点击漏到了真实棋盘，
            # 一定会产生一条 move_intent，从而被下面的断言抓到。
            self.assertTrue(session.accepts_human_input)
            sent_before = len(transport.sent_messages)
            self.assertEqual(sent_before, 0, "打开沙盘本身不应产生任何网络消息")

            # 在沙盘棋盘上模拟一次点击。
            panel.sandbox_panel.board_widget.cell_clicked.emit(7, 7)

            # 服务器一条消息都没收到。
            self.assertEqual(len(transport.sent_messages), sent_before)
            self.assertEqual(transport.sent_messages, [])

            # 但沙盘里确实多了一手。
            model = panel.sandbox_panel.model
            self.assertIsNotNone(model)
            self.assertEqual(len(model.moves), 1)
            self.assertIsNotNone(model.board[7][7])
            # 基线局面是黑方行棋，沙盘里的第一手就是黑子。
            self.assertEqual(model.board[7][7], Player.BLACK)

            # 真实棋盘没有被动过。
            self.assertIsNone(panel.board_widget._view.board[7][7])
        finally:
            self._teardown(panel, session, transport)

    def test_undo_and_reset_in_the_sandbox(self) -> None:
        """撤销与“回到当前局面”都要正确作用在沙盘上。"""
        panel, session, transport = self._panel()
        try:
            panel.sandbox_button.click()
            model = panel.sandbox_panel.model
            self.assertIsNotNone(model)

            # 落一手再撤销。
            panel.sandbox_panel.board_widget.cell_clicked.emit(7, 7)
            self.assertEqual(len(model.moves), 1)
            panel.sandbox_panel.undo_button.click()
            self.assertEqual(len(model.moves), 0)
            self.assertIsNone(model.board[7][7])
            self.assertFalse(model.has_sandbox_moves)

            # 落两手再回到当前局面。
            panel.sandbox_panel.board_widget.cell_clicked.emit(3, 3)
            panel.sandbox_panel.board_widget.cell_clicked.emit(4, 4)
            self.assertEqual(len(model.moves), 2)
            panel.sandbox_panel.reset_button.click()
            self.assertEqual(len(model.moves), 0)
            self.assertIsNone(model.board[3][3])
            self.assertIsNone(model.board[4][4])
            self.assertFalse(model.has_sandbox_moves)
            # 沙盘回到基线：基线手数不变，棋盘回到真实局面的样子。
            self.assertEqual(model.move_count, model.base_move_count)
        finally:
            self._teardown(panel, session, transport)

    def test_closing_the_sandbox_restores_the_real_board(self) -> None:
        """退出沙盘后真实棋盘要恢复可交互。"""
        # 轮到自己，真实棋盘本来应该是可交互的。
        panel, session, transport = self._panel(current="black")
        try:
            self.assertTrue(session.accepts_human_input)
            self.assertTrue(panel.board_widget._interaction_enabled)

            panel.sandbox_button.click()
            self.assertTrue(panel._sandbox_open)
            self.assertFalse(panel.board_widget._interaction_enabled)

            panel.sandbox_button.click()
            self.assertFalse(panel._sandbox_open)
            self.assertFalse(panel.sandbox_panel.isVisible())
            self.assertEqual(panel.sandbox_button.text(), "沙盘推演")
            # 真实棋盘的交互状态重新按会话计算。
            self.assertTrue(panel.board_widget._interaction_enabled)
            self.assertEqual(
                panel.board_widget._interaction_enabled,
                session.accepts_human_input,
            )
        finally:
            self._teardown(panel, session, transport)

    def test_sandbox_reports_when_the_real_game_advances(self) -> None:
        """真实对局推进时，沙盘要提示局面已经更新。"""
        panel, session, transport = self._panel()
        try:
            panel.sandbox_button.click()
            self.assertFalse(panel.sandbox_panel.base_stale)

            # 服务器下发了手数更高的新局面。
            transport.receive(
                make_state_message(current="white", move_no=5)
            )

            self.assertTrue(panel.sandbox_panel.base_stale)
            self.assertIn("已更新", panel.sandbox_panel.status_label.text())
        finally:
            self._teardown(panel, session, transport)

    def test_sandbox_button_is_disabled_without_a_game(self) -> None:
        """没有对局时不能打开沙盘。"""
        panel, session, transport = self._panel(with_game=False)
        try:
            self.assertFalse(panel.sandbox_button.isEnabled())
            panel.sandbox_button.click()
            self.assertFalse(panel._sandbox_open)
            self.assertFalse(panel.sandbox_panel.isVisible())
            self.assertFalse(panel.sandbox_panel.is_loaded)
        finally:
            self._teardown(panel, session, transport)

    def test_sandbox_notes_when_the_real_game_is_finished(self) -> None:
        """真实对局已经终局时，沙盘要说明基线本身已分出胜负。"""
        panel, session, transport = self._panel()
        try:
            # 服务器下发一局已终局的快照：黑棋五连。
            payload = dict(
                state_message(
                    your_color="black",
                    current="white",
                    move_no=5,
                    status="black_won",
                )
            )
            payload["payload"]["board"] = board_with_stones({
                (7, 5): "black",
                (7, 6): "black",
                (7, 7): "black",
                (7, 8): "black",
                (7, 9): "black",
            })
            payload["payload"]["winning_line"] = [
                [7, 5],
                [7, 6],
                [7, 7],
                [7, 8],
                [7, 9],
            ]
            transport.receive(payload)

            panel.sandbox_button.click()
            self.assertTrue(panel._sandbox_open)
            # 终局局面照样能推演，但状态栏要说清楚基线已经结束。
            self.assertIn("真实对局已终局", panel.sandbox_panel.status_label.text())
            panel.sandbox_panel.board_widget.cell_clicked.emit(3, 3)
            self.assertEqual(len(panel.sandbox_panel.model.moves), 1)
        finally:
            self._teardown(panel, session, transport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
