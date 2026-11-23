"""联机倒计时与超时判负的客户端测试。

覆盖三件事：
* 本手倒计时用醒目的大号数字显示，最后 10 秒转红；
* 最后 10 秒逐秒响一次“咚”提示音（只在自己的回合响）；
* 超时判负后，胜方与负方各自弹出对应的提示窗口。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.online_pages.common import (  # noqa: E402
    online_timeout_notice,
)
from gomoku.client.online_panel import OnlinePanel  # noqa: E402
from gomoku.client.sessions.online import OnlineGameSession  # noqa: E402
from gomoku.shared.protocol import (  # noqa: E402
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    make_message,
)
from gomoku.shared.rules import BOARD_SIZE, Player  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


def blank_board() -> list[list[str | None]]:
    return [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def state_message(
    *,
    your_color: str | None = "black",
    current: str = "black",
    status: str = "playing",
    result_reason: str = "",
    game_id: str = "game-1",
    turn_deadline_ms: int | None = None,
    server_now_ms: int | None = None,
) -> dict:
    """构造一条符合协议的服务器局面消息。"""
    return make_message(
        SERVER_GAME_STATE,
        {
            "game_id": game_id,
            "board": blank_board(),
            "current_player": current,
            "your_color": your_color,
            "move_no": 0,
            "status": status,
            "last_move": None,
            "winning_line": [],
            "rated": False,
            "result_reason": result_reason,
            "turn_deadline_ms": turn_deadline_ms,
            "server_now_ms": server_now_ms,
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


class CountdownDisplayTests(unittest.TestCase):
    """本手倒计时的大号数字与最后 10 秒的红色状态。"""

    def _panel(
        self,
        *,
        current: str = "black",
        your_color: str = "black",
    ) -> tuple[OnlinePanel, OnlineGameSession, FakeTransport]:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        transport.receive(
            state_message(your_color=your_color, current=current)
        )
        panel = OnlinePanel()
        panel._session = session
        panel._ws_connected = True
        panel._show_game_page()
        panel._sync_game_page()
        return panel, session, transport

    def test_countdown_shows_a_big_number(self) -> None:
        """倒计时用单独的大号数字显示，不是藏在状态文字里。"""
        panel, _session, _transport = self._panel()
        try:
            panel._deadline_monotonic = time.monotonic() + 25.0
            panel._update_clock_label()
            self.assertFalse(panel.countdown_label.isHidden())
            self.assertEqual(panel.countdown_label.text(), "25")
            self.assertEqual(panel.clock_label.text(), "本手倒计时")
            self.assertEqual(
                panel.countdown_label.property("urgent"), "false"
            )
        finally:
            panel.close()

    def test_last_ten_seconds_turn_red(self) -> None:
        """最后 10 秒数字转红。"""
        panel, _session, _transport = self._panel()
        try:
            panel._deadline_monotonic = time.monotonic() + 10.5
            panel._update_clock_label()
            self.assertEqual(panel.countdown_label.text(), "11")
            self.assertEqual(
                panel.countdown_label.property("urgent"), "false"
            )

            panel._deadline_monotonic = time.monotonic() + 9.5
            panel._update_clock_label()
            self.assertEqual(panel.countdown_label.text(), "10")
            self.assertEqual(
                panel.countdown_label.property("urgent"), "true"
            )
        finally:
            panel.close()

    def test_countdown_hidden_without_deadline(self) -> None:
        """没有倒计时时收起大号数字。"""
        panel, _session, _transport = self._panel()
        try:
            panel._deadline_monotonic = time.monotonic() + 25.0
            panel._update_clock_label()
            self.assertFalse(panel.countdown_label.isHidden())

            panel._deadline_monotonic = None
            panel._update_clock_label()
            self.assertTrue(panel.countdown_label.isHidden())
        finally:
            panel.close()


class CountdownSoundTests(unittest.TestCase):
    """最后 10 秒的“咚”提示音。"""

    def _panel(
        self,
        *,
        current: str = "black",
        your_color: str = "black",
    ) -> tuple[OnlinePanel, OnlineGameSession, FakeTransport]:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        transport.receive(
            state_message(
                your_color=your_color,
                current=current,
                turn_deadline_ms=30_000,
                server_now_ms=0,
            )
        )
        panel = OnlinePanel()
        panel._session = session
        panel._ws_connected = True
        panel._show_game_page()
        panel._sync_game_page()
        return panel, session, transport

    def test_ten_beeps_in_the_last_ten_seconds(self) -> None:
        """10、9……1 秒各响一次，正好十声。"""
        panel, _session, _transport = self._panel()
        audio = mock.Mock()
        try:
            with mock.patch(
                "gomoku.client.online_panel.get_audio_manager",
                return_value=audio,
            ):
                for seconds in range(10, 0, -1):
                    panel._deadline_monotonic = (
                        time.monotonic() + seconds - 0.5
                    )
                    panel._update_clock_label()
            self.assertEqual(audio.play_tick.call_count, 10)
        finally:
            panel.close()

    def test_same_second_beeps_only_once(self) -> None:
        """同一秒里刷新多次也只能响一声。"""
        panel, _session, _transport = self._panel()
        audio = mock.Mock()
        try:
            with mock.patch(
                "gomoku.client.online_panel.get_audio_manager",
                return_value=audio,
            ):
                panel._deadline_monotonic = time.monotonic() + 9.5
                panel._update_clock_label()
                panel._update_clock_label()
                panel._update_clock_label()
            self.assertEqual(audio.play_tick.call_count, 1)
        finally:
            panel.close()

    def test_no_beep_outside_the_last_ten_seconds(self) -> None:
        """还早的时候不响提示音。"""
        panel, _session, _transport = self._panel()
        audio = mock.Mock()
        try:
            with mock.patch(
                "gomoku.client.online_panel.get_audio_manager",
                return_value=audio,
            ):
                panel._deadline_monotonic = time.monotonic() + 25.0
                panel._update_clock_label()
            audio.play_tick.assert_not_called()
        finally:
            panel.close()

    def test_no_beep_when_waiting_for_the_opponent(self) -> None:
        """等对手落子时不响：那是对手的钟在走。"""
        panel, _session, _transport = self._panel(current="white")
        audio = mock.Mock()
        try:
            with mock.patch(
                "gomoku.client.online_panel.get_audio_manager",
                return_value=audio,
            ):
                panel._deadline_monotonic = time.monotonic() + 9.5
                panel._update_clock_label()
            audio.play_tick.assert_not_called()
        finally:
            panel.close()

    def test_new_turn_restarts_the_beeps(self) -> None:
        """进入新的一手后，提示音要能重新开始。"""
        panel, session, transport = self._panel()
        audio = mock.Mock()
        try:
            with mock.patch(
                "gomoku.client.online_panel.get_audio_manager",
                return_value=audio,
            ):
                panel._deadline_monotonic = time.monotonic() + 9.5
                panel._update_clock_label()
                self.assertEqual(audio.play_tick.call_count, 1)

                # 同一手：服务器又推了一份快照，截止时间没变，节拍不能重来。
                transport.receive(
                    state_message(
                        your_color="black",
                        current="black",
                        turn_deadline_ms=30_000,
                        server_now_ms=0,
                    )
                )
                panel._sync_game_page()
                panel._deadline_monotonic = time.monotonic() + 9.5
                panel._update_clock_label()
                self.assertEqual(audio.play_tick.call_count, 1)

                # 新的一手：截止时间变了，节拍重新开始。
                transport.receive(
                    state_message(
                        your_color="black",
                        current="black",
                        turn_deadline_ms=60_000,
                        server_now_ms=0,
                    )
                )
                panel._sync_game_page()
                panel._deadline_monotonic = time.monotonic() + 9.5
                panel._update_clock_label()
                self.assertEqual(audio.play_tick.call_count, 2)
            self.assertIsNotNone(session.snapshot)
        finally:
            panel.close()


class TimeoutNoticeTests(unittest.TestCase):
    """超时判负后双方都要看到对应的提示弹窗。"""

    def _snapshot(
        self,
        *,
        winner: Player,
        your_color: Player,
        reason: str,
    ):
        from gomoku.shared.protocol import GameSnapshot

        return GameSnapshot(
            game_id="g-1",
            board=tuple(
                tuple(None for _ in range(BOARD_SIZE))
                for _ in range(BOARD_SIZE)
            ),
            current_player=Player.BLACK,
            last_move=None,
            winner=winner,
            winning_line=(),
            is_draw=False,
            is_game_over=True,
            move_no=1,
            your_color=your_color,
            result_reason=reason,
        )

    def test_winner_and_loser_get_different_notices(self) -> None:
        """胜方看到“对手超时”，负方看到“本手超时”。"""
        self.assertEqual(
            online_timeout_notice(
                self._snapshot(
                    winner=Player.BLACK,
                    your_color=Player.BLACK,
                    reason="timeout",
                )
            ),
            ("对手超时", "对手本手超时未落子，你获胜！"),
        )
        self.assertEqual(
            online_timeout_notice(
                self._snapshot(
                    winner=Player.WHITE,
                    your_color=Player.BLACK,
                    reason="timeout",
                )
            ),
            ("本手超时", "本手超时未落子，本局判负。"),
        )

    def test_other_endings_have_no_timeout_notice(self) -> None:
        """不是超时结束的对局不弹超时提示。"""
        for reason in ("five", "resign", "escape", "draw", ""):
            with self.subTest(reason=reason):
                self.assertIsNone(
                    online_timeout_notice(
                        self._snapshot(
                            winner=Player.BLACK,
                            your_color=Player.BLACK,
                            reason=reason,
                        )
                    )
                )

    def _panel(self, your_color: str) -> tuple[
        OnlinePanel, OnlineGameSession, FakeTransport
    ]:
        """造一个正在进行、随时可能超时的对局面板。"""
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        transport.receive(state_message(your_color=your_color))
        panel = OnlinePanel()
        panel._session = session
        panel._ws_connected = True
        # 走真实链路：会话判定终局 → 面板弹出提示。
        session.game_over.connect(panel._on_online_game_over)
        panel._show_game_page()
        panel._sync_game_page()
        return panel, session, transport

    def _finish_by_timeout(
        self, transport: FakeTransport, your_color: str
    ) -> None:
        """服务器判定本局因超时结束。"""
        transport.receive(
            state_message(
                your_color=your_color,
                status="black_won",
                result_reason="timeout",
            )
        )
        QApplication.instance().processEvents()

    def test_timeout_popup_shows_for_the_loser(self) -> None:
        """超时判负的一方弹出“本手超时”。"""
        panel, _session, transport = self._panel(your_color="white")
        try:
            panel.show()
            QApplication.instance().processEvents()
            with mock.patch(
                "gomoku.client.online_panel.NoticeOverlay"
            ) as overlay:
                self._finish_by_timeout(transport, your_color="white")
            overlay.assert_called_once()
            args, _kwargs = overlay.call_args
            self.assertEqual(args[0], "本手超时")
            self.assertEqual(args[1], "本手超时未落子，本局判负。")
            self.assertEqual(overlay.call_args.kwargs["seal_text"], "负")
        finally:
            panel.close()

    def test_timeout_popup_shows_for_the_winner(self) -> None:
        """因对手超时获胜的一方弹出“对手超时”。"""
        panel, _session, transport = self._panel(your_color="black")
        try:
            panel.show()
            QApplication.instance().processEvents()
            with mock.patch(
                "gomoku.client.online_panel.NoticeOverlay"
            ) as overlay:
                self._finish_by_timeout(transport, your_color="black")
            overlay.assert_called_once()
            args, _kwargs = overlay.call_args
            self.assertEqual(args[0], "对手超时")
            self.assertEqual(args[1], "对手本手超时未落子，你获胜！")
            self.assertEqual(overlay.call_args.kwargs["seal_text"], "胜")
        finally:
            panel.close()

    def test_timeout_popup_shows_only_once(self) -> None:
        """同一局只弹一次。"""
        panel, _session, transport = self._panel(your_color="black")
        try:
            panel.show()
            QApplication.instance().processEvents()
            with mock.patch(
                "gomoku.client.online_panel.NoticeOverlay"
            ) as overlay:
                self._finish_by_timeout(transport, your_color="black")
                # 再触发一次（比如又收到一份终局快照），不能再弹。
                panel._show_online_timeout_notice()
            self.assertEqual(overlay.call_count, 1)
        finally:
            panel.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
