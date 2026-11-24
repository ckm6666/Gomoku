"""联机面板反馈回归测试。

覆盖四件事：

* 落子被服务器拒绝后，原因必须停留几秒，不能被紧接着的状态同步清掉；
* 面板被切到后台时停掉界面定时器与终局提示，回到前台再恢复；
* 同意悔棋后按钮立即禁用，等服务器确认后才重新可用；
* 密码没能安全保存时，“记住密码”复选框必须跟着取消勾选。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QObject, QSettings, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gomoku.client.online_panel as online_panel_module  # noqa: E402
from gomoku.client.online_panel import OnlinePanel  # noqa: E402
from gomoku.client.sessions.online import OnlineGameSession  # noqa: E402
from gomoku.shared.protocol import (  # noqa: E402
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    make_message,
)
from gomoku.shared.rules import BOARD_SIZE  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


def blank_board() -> list[list[str | None]]:
    return [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def state_message(
    *,
    your_color: str = "black",
    current: str = "black",
    status: str = "playing",
    result_reason: str = "",
    game_id: str = "game-1",
    board: list[list[str | None]] | None = None,
    move_no: int = 0,
) -> dict:
    """构造一条符合协议的服务器局面消息。"""
    return make_message(
        SERVER_GAME_STATE,
        {
            "game_id": game_id,
            "board": board if board is not None else blank_board(),
            "current_player": current,
            "your_color": your_color,
            "move_no": move_no,
            "status": status,
            "last_move": None,
            "winning_line": [],
            "rated": False,
            "result_reason": result_reason,
            "turn_deadline_ms": None,
            "server_now_ms": None,
            "black_name": "张三",
            "white_name": "李四",
            "black_user_id": "u-black",
            "white_user_id": "u-white",
        },
    )


def authenticate(transport: FakeTransport, session: OnlineGameSession) -> None:
    """让会话进入已认证状态。"""
    transport.receive(
        make_message(
            SERVER_AUTHENTICATED,
            {"user": {"id": "u3", "username": "玩家"}},
        )
    )


def wait_until(predicate, timeout: float = 3.0) -> bool:
    """处理 Qt 事件直到条件成立或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QT_APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QT_APP.processEvents()
    return bool(predicate())


class _FakeUndoSession(QObject):
    """只提供悔棋相关接口的假会话。"""

    undo_request_received = Signal(str)
    undo_status_changed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.undo_pending = True
        self.undo_requested_by_you = False
        self.undo_remaining = 1
        self.undo_limit = 2
        self.undo_cooldown_seconds = 30
        self.undo_cooldown_remaining = 0
        self.can_undo = True
        self.responses: list[bool] = []

    def respond_undo(self, accept: bool) -> bool:
        self.responses.append(bool(accept))
        return True

    def request_undo(self) -> bool:
        return False


class MoveRejectionFeedbackTests(unittest.TestCase):
    """落子被拒的原因必须留在错误标签上。"""

    def _panel(self):
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        transport.receive(state_message())
        panel = OnlinePanel()
        panel._session = session
        panel._ws_connected = True
        session.error.connect(panel._on_online_error)
        panel._show_game_page()
        panel._sync_game_page()
        return panel, session, transport

    def test_reason_survives_state_sync_and_disappears_later(self) -> None:
        """状态同步不能抹掉失败原因；停留时间到了提示才消失。"""
        panel, session, transport = self._panel()
        try:
            with mock.patch.object(
                online_panel_module, "GAME_ERROR_HOLD_SECONDS", 0.2
            ):
                # 服务器拒绝落子：会话发出 error。
                session.error.emit("该位置已有棋子")
                self.assertEqual(
                    panel.game_error_label.text(), "该位置已有棋子"
                )

                # 紧接着来的状态同步不能把原因抹掉。
                transport.receive(state_message())
                panel._sync_game_page()
                self.assertEqual(
                    panel.game_error_label.text(), "该位置已有棋子"
                )

                # 停留时间到了之后提示自动消失。
                self.assertTrue(
                    wait_until(
                        lambda: not panel.game_error_label.text(), 3.0
                    )
                )
                self.assertEqual(panel.game_error_label.text(), "")
        finally:
            panel.close()

    def test_reason_is_written_to_chat_log(self) -> None:
        """失败原因同时进对话日志，方便回看。"""
        panel, session, _transport = self._panel()
        try:
            session.error.emit("还没有轮到你")
            self.assertTrue(
                wait_until(
                    lambda: panel.chat_log.entry_count() > 0, 2.0
                )
            )
            self.assertTrue(
                any(
                    "还没有轮到你" in text
                    for text in panel.chat_log.entry_texts()
                )
            )
        finally:
            panel.close()


class VisibleStateLifecycleTests(unittest.TestCase):
    """面板隐藏时暂停界面活动，回到前台再恢复。"""

    def test_pause_and_resume_visible_state(self) -> None:
        panel = OnlinePanel()
        try:
            panel._show_game_page()
            self.assertTrue(panel._clock_timer.isActive())

            panel.pause_visible_state()
            self.assertFalse(panel._clock_timer.isActive())
            self.assertFalse(panel._live_refresh_timer.isActive())
            self.assertTrue(panel._visible_paused)

            panel.resume_visible_state()
            self.assertTrue(panel._clock_timer.isActive())
            self.assertFalse(panel._visible_paused)
        finally:
            panel.close()

    def test_plaza_refresh_timer_follows_visibility(self) -> None:
        """广场的定时刷新同样跟随可见状态。"""
        panel = OnlinePanel()
        try:
            panel._show_plaza_page()
            self.assertTrue(panel._live_refresh_timer.isActive())

            panel.pause_visible_state()
            self.assertFalse(panel._live_refresh_timer.isActive())

            panel.resume_visible_state()
            self.assertTrue(panel._live_refresh_timer.isActive())
        finally:
            panel.close()

    def test_end_of_game_sound_is_suppressed_while_hidden(self) -> None:
        """面板隐藏时不放终局音；回到前台后才补上。"""
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        board = blank_board()
        for col in range(3, 8):
            board[7][col] = "black"
        transport.receive(
            state_message(
                status="black_won",
                result_reason="five",
                board=board,
                move_no=5,
            )
        )
        panel = OnlinePanel()
        panel._session = session
        panel._ws_connected = True
        audio = mock.Mock()
        try:
            with mock.patch(
                "gomoku.client.online_panel.get_audio_manager",
                return_value=audio,
            ):
                panel.pause_visible_state()
                panel._play_online_result_sound()
                audio.play_win.assert_not_called()
                audio.play_lose.assert_not_called()
                audio.play_draw.assert_not_called()

                panel.resume_visible_state()
                panel._play_online_result_sound()
                audio.play_win.assert_called_once()
        finally:
            panel.close()


class UndoResponseTests(unittest.TestCase):
    """同意悔棋后按钮立即禁用，等服务器确认。"""

    def test_agree_button_is_disabled_until_server_confirms(self) -> None:
        panel = OnlinePanel()
        try:
            session = _FakeUndoSession()
            panel._session = session
            panel._ws_connected = True

            panel._on_undo_clicked()
            self.assertEqual(session.responses, [True])
            self.assertFalse(panel.undo_button.isEnabled())

            # 网络往返期间再点一次：不能重复发送“同意”。
            panel._on_undo_clicked()
            self.assertEqual(session.responses, [True])

            # 服务器广播结果后按钮恢复可用。
            session.undo_pending = False
            panel._undo_responded = False
            panel._refresh_undo_controls()
            self.assertTrue(panel.undo_button.isEnabled())
        finally:
            panel.close()

    def test_button_is_re_enabled_when_request_is_declined(self) -> None:
        """对手拒绝或请求取消后，按钮要能重新使用。"""
        panel = OnlinePanel()
        try:
            session = _FakeUndoSession()
            panel._session = session
            panel._ws_connected = True
            session.undo_status_changed.connect(
                panel._on_undo_status_changed
            )

            panel._on_undo_clicked()
            self.assertTrue(panel._undo_responded)

            with mock.patch.object(
                panel, "_sync_game_page", lambda: None
            ):
                session.undo_status_changed.emit(
                    "declined", "对手拒绝了悔棋请求"
                )
            self.assertFalse(panel._undo_responded)
        finally:
            panel.close()


class RememberPasswordUiTests(unittest.TestCase):
    """密码没能安全保存时，复选框必须取消勾选。"""

    def test_checkbox_is_unchecked_when_password_cannot_be_stored(self) -> None:
        panel = OnlinePanel()
        try:
            with tempfile.TemporaryDirectory() as directory:
                panel._settings = QSettings(
                    str(Path(directory) / "settings.ini"),
                    QSettings.Format.IniFormat,
                )
                panel.remember_password_checkbox.setChecked(True)
                with mock.patch.object(
                    online_panel_module, "save_login", return_value=False
                ), mock.patch.object(
                    online_panel_module, "clear_login"
                ) as mock_clear:
                    warning = panel._save_saved_login("alice")
                self.assertTrue(warning)
                self.assertFalse(
                    panel.remember_password_checkbox.isChecked()
                )
                # 用户名要留下：不能顺手把刚记住的用户名也删掉。
                mock_clear.assert_not_called()
        finally:
            panel.close()

    def test_checkbox_stays_checked_when_password_is_stored(self) -> None:
        panel = OnlinePanel()
        try:
            with tempfile.TemporaryDirectory() as directory:
                panel._settings = QSettings(
                    str(Path(directory) / "settings.ini"),
                    QSettings.Format.IniFormat,
                )
                panel.remember_password_checkbox.setChecked(True)
                with mock.patch.object(
                    online_panel_module, "save_login", return_value=True
                ):
                    warning = panel._save_saved_login("alice")
                self.assertEqual(warning, "")
                self.assertTrue(
                    panel.remember_password_checkbox.isChecked()
                )
        finally:
            panel.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
