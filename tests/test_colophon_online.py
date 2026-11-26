"""联机对局的题跋拓片测试（离屏）。

联机这一路和本地不一样：快照只有终局局面，落子次序得向服务端要；
本地也没有 AI 复盘，题跋只能自动起草。这里覆盖这两处特殊之处。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.colophon import (  # noqa: E402
    ColophonStore,
    draft_online_colophons,
)
from gomoku.client.colophon_editor import ColophonEditor  # noqa: E402
from gomoku.client.main_window import MainWindow  # noqa: E402
from gomoku.client.online_panel import OnlinePanel  # noqa: E402
from gomoku.client.records import LocalRecordStore  # noqa: E402
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402
from gomoku.shared.record import GameRecord  # noqa: E402
from gomoku.shared.rules import Move, Player  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])
apply_app_theme(QT_APP)


def _moves() -> list[dict[str, object]]:
    """黑棋第 9 手成五的一局，落子序列。"""
    black = [(7, 2), (7, 3), (7, 4), (7, 5), (7, 6)]
    white = [(9, 9), (10, 9), (11, 9), (12, 9)]
    sequence: list[tuple[int, int]] = []
    for index in range(5):
        sequence.append(black[index])
        if index < len(white):
            sequence.append(white[index])
    return [
        {
            "row": row,
            "col": col,
            "color": "black" if index % 2 == 0 else "white",
        }
        for index, (row, col) in enumerate(sequence)
    ]


def _game_detail(
    *,
    game_id: str = "g1",
    winner_user_id: str = "u1",
    your_color: str = "black",
    result_reason: str = "five",
) -> dict[str, object]:
    return {
        "game_id": game_id,
        "moves": _moves(),
        "status": "black_won",
        "winner_user_id": winner_user_id,
        "black_user_id": "u1",
        "white_user_id": "u2",
        "black_username": "墨客",
        "white_username": "松风",
        "your_color": your_color,
        "result_reason": result_reason,
        "rule_mode": "freestyle",
        "rated": True,
    }


def _online_record(
    *,
    winner: Player | None = Player.BLACK,
    result_reason: str = "five",
) -> GameRecord:
    moves = tuple(
        Move(
            int(item["row"]),
            int(item["col"]),
            Player.BLACK if item["color"] == "black" else Player.WHITE,
        )
        for item in _moves()
    )
    return GameRecord(
        record_id="online-g1",
        created_at="2026-11-26T14:30:00+00:00",
        mode="online",
        moves=moves,
        winner=winner,
        result_reason=result_reason,
        black_name="墨客",
        white_name="松风",
        rated=True,
        metadata={"your_color": "black"},
    )


class DraftOnlineColophonsTests(unittest.TestCase):
    """联机题跋的自动起草。"""

    def test_returns_three_voices(self) -> None:
        drafts = draft_online_colophons(
            _online_record(),
            your_color=Player.BLACK,
            finished_by_five=True,
        )
        self.assertEqual(len(drafts), 3)
        for _voice, text in drafts:
            self.assertTrue(text.strip())

    def test_win_is_called_a_win(self) -> None:
        drafts = draft_online_colophons(
            _online_record(),
            your_color=Player.BLACK,
            finished_by_five=True,
        )
        plain = drafts[0][1]
        self.assertIn("胜", plain)
        self.assertNotIn("惜败", plain)

    def test_loss_is_regretted(self) -> None:
        record = _online_record(winner=Player.WHITE)
        drafts = draft_online_colophons(
            record,
            your_color=Player.BLACK,
            finished_by_five=True,
        )
        self.assertIn("惜败", drafts[0][1])

    def test_draw(self) -> None:
        record = _online_record(winner=None, result_reason="draw")
        drafts = draft_online_colophons(record, your_color=Player.BLACK)
        self.assertIn("和棋", drafts[0][1])

    def test_five_in_a_row_is_mentioned(self) -> None:
        drafts = draft_online_colophons(
            _online_record(),
            your_color=Player.BLACK,
            finished_by_five=True,
        )
        self.assertIn("五连", drafts[1][1])

    def test_resignation_is_not_called_five(self) -> None:
        record = _online_record(result_reason="resign")
        drafts = draft_online_colophons(
            record,
            your_color=Player.BLACK,
            finished_by_five=False,
        )
        self.assertNotIn("五连", drafts[1][1])

    def test_hand_count_is_included(self) -> None:
        drafts = draft_online_colophons(
            _online_record(),
            your_color=Player.BLACK,
            finished_by_five=True,
        )
        self.assertIn("第 9 手", drafts[0][1])

    def test_empty_game_does_not_crash(self) -> None:
        record = GameRecord(
            record_id="empty",
            created_at="2026-11-26T14:30:00+00:00",
            mode="online",
            moves=(),
            winner=None,
            result_reason="",
        )
        drafts = draft_online_colophons(record, your_color=Player.BLACK)
        self.assertEqual(len(drafts), 3)


class _FakeSnapshot:
    def __init__(self, *, game_id: str = "g1") -> None:
        self.game_id = game_id
        self.is_game_over = True
        self.your_color = Player.BLACK
        self.is_spectator = False


class _FakeSession:
    """够用的假会话。

    面板的定时器会顺手读会话上的各种属性（观战标记、时钟等），这里对
    未定义的属性一律返回 Mock，免得测试被无关的读取打断。
    """

    def __init__(self) -> None:
        self.snapshot = _FakeSnapshot()
        self.is_spectator = False

    def supports(self, _capability: str) -> bool:
        return False

    def __getattr__(self, name: str) -> object:
        return mock.Mock()


class OnlineColophonFlowTests(unittest.TestCase):
    """联机面板的拓片流程。"""

    def setUp(self) -> None:
        self.panel = OnlinePanel()
        self.panel._session = _FakeSession()  # type: ignore[assignment]
        self.panel._auth_session = mock.Mock()  # type: ignore[assignment]
        self.panel._auth_session.access_token = "token-1"

    def tearDown(self) -> None:
        self.panel.deleteLater()

    def test_clicking_fetches_the_game_detail(self) -> None:
        with mock.patch.object(
            self.panel._history_client, "fetch_game"
        ) as fetch:
            self.panel._on_colophon_clicked()
        fetch.assert_called_once_with("token-1", "g1")
        self.assertTrue(self.panel._pending_colophon)

    def test_clicking_does_nothing_before_the_game_is_over(self) -> None:
        self.panel._session.snapshot.is_game_over = False  # type: ignore[attr-defined]
        with mock.patch.object(
            self.panel._history_client, "fetch_game"
        ) as fetch:
            self.panel._on_colophon_clicked()
        fetch.assert_not_called()
        self.assertFalse(self.panel._pending_colophon)

    def test_detail_is_turned_into_a_colophon_request(self) -> None:
        received: list[object] = []
        self.panel.colophon_requested.connect(received.append)

        # 走真实顺序：先点「拓片」，棋谱回来时才认得出这是拓片要的。
        with mock.patch.object(
            self.panel._history_client, "fetch_game"
        ):
            self.panel._on_colophon_clicked()
        self.panel._on_history_game_ready(_game_detail())

        self.assertEqual(len(received), 1)
        record, your_color, finished_by_five = received[0]
        self.assertIsInstance(record, GameRecord)
        self.assertEqual(record.black_name, "墨客")
        self.assertEqual(len(record.moves), 9)
        self.assertIs(your_color, Player.BLACK)
        self.assertTrue(finished_by_five)
        self.assertFalse(self.panel._pending_colophon)

    def test_a_failed_fetch_does_not_leave_the_flag_stuck(self) -> None:
        self.panel._pending_colophon = True
        self.panel._on_history_failed("网络不通")
        self.assertFalse(self.panel._pending_colophon)

    def test_invalid_detail_reports_instead_of_emitting(self) -> None:
        received: list[object] = []
        self.panel.colophon_requested.connect(received.append)
        with mock.patch.object(
            self.panel._history_client, "fetch_game"
        ):
            self.panel._on_colophon_clicked()
        # 缺少落子数组：不是合法的棋谱。
        self.panel._on_history_game_ready({"game_id": "g1"})
        self.assertEqual(received, [])
        self.assertFalse(self.panel._pending_colophon)

    def test_a_replay_response_is_not_taken_for_the_colophon(self) -> None:
        """复盘与拓片共用取棋谱的接口，两个请求可以同时在途。

        回来的若是别的对局，得交给复盘去处理：硬当成拓片会把复盘吞掉，
        拓片也会拿到别人的棋谱。
        """
        received: list[object] = []
        self.panel.colophon_requested.connect(received.append)
        replayed: list[object] = []
        self.panel._profile_ctrl.on_history_game_ready = replayed.append

        with mock.patch.object(
            self.panel._history_client, "fetch_game"
        ):
            self.panel._on_colophon_clicked()

        # 别的对局的棋谱先回来了。
        self.panel._on_history_game_ready(_game_detail(game_id="g2"))

        self.assertEqual(received, [])
        self.assertEqual(len(replayed), 1)
        # 拓片还在等着自己那一局。
        self.assertTrue(self.panel._pending_colophon)

        # 自己那一局回来时才交给拓片。
        self.panel._on_history_game_ready(_game_detail(game_id="g1"))
        self.assertEqual(len(received), 1)
        self.assertFalse(self.panel._pending_colophon)


class MainWindowOnlineColophonTests(unittest.TestCase):
    """主窗口收到联机拓片请求后自动生成题跋。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.window = MainWindow(
            record_store=LocalRecordStore(
                Path(self._tmpdir.name) / "records"
            ),
            colophon_store=ColophonStore(
                Path(self._tmpdir.name) / "colophons"
            ),
            auto_update_check=False,
        )

    def tearDown(self) -> None:
        self.window.close()
        QT_APP.processEvents()
        self._tmpdir.cleanup()

    def test_request_opens_an_editor_prefilled_with_a_draft(self) -> None:
        opened: list[ColophonEditor] = []

        def _capture(_self, record, **kwargs):  # type: ignore[no-untyped-def]
            editor = ColophonEditor(
                record, parent=self.window._overlay_host(), **kwargs
            )
            opened.append(editor)
            return editor

        with mock.patch.object(
            MainWindow, "_open_colophon_editor", _capture
        ):
            self.window._open_online_colophon(
                (_online_record(), Player.BLACK, True)
            )

        self.assertEqual(len(opened), 1)
        editor = opened[0]
        # 自动生成的那句已经落在正文里，玩家可以直接存册或改写。
        self.assertIn("第 9 手", editor.text_edit.toPlainText())
        self.assertIn("胜", editor.text_edit.toPlainText())
        # 点睛的是最后一手。
        self.assertEqual(editor.preview.colophon().ply, 9)
        self.assertEqual(editor.preview.colophon().focus(), (7, 6))

    def test_bad_payload_is_ignored(self) -> None:
        with mock.patch.object(
            MainWindow, "_open_colophon_editor"
        ) as opener:
            self.window._open_online_colophon("不是元组")
            self.window._open_online_colophon((1, 2))
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
