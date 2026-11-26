"""题跋拓片在主窗口里的接线测试（离屏）。

从复盘页点「拓片册」应切到册页；册页关闭应回复盘页；终局弹窗的
「拓片」应在模态层关闭之后才开编辑器。这些都是只有跑起来才暴露的
接线问题。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.colophon import ColophonStore  # noqa: E402
from gomoku.client.colophon_album import ColophonAlbum  # noqa: E402
from gomoku.client.main_window import MainWindow  # noqa: E402
from gomoku.client.records import LocalRecordStore  # noqa: E402
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])
QT_APP.setStyle("Fusion")
apply_app_theme(QT_APP)


class MainWindowColophonTests(unittest.TestCase):
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
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        self.window.show()
        QT_APP.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        QT_APP.processEvents()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # 册页
    # ------------------------------------------------------------------
    def test_album_opens_from_the_replay_page(self) -> None:
        self.window._open_replay_page()
        QT_APP.processEvents()
        panel = self.window._replay_panel
        self.assertIsNotNone(panel)

        panel.album_requested.emit()
        QT_APP.processEvents()

        album = self.window._colophon_album
        self.assertIsNotNone(album)
        self.assertIsInstance(album, ColophonAlbum)
        self.assertIs(self.window.stack.currentWidget(), album)

    def test_album_closes_back_to_the_replay_page(self) -> None:
        self.window._open_replay_page()
        QT_APP.processEvents()
        self.window._open_colophon_album()
        QT_APP.processEvents()

        self.window._colophon_album.closed.emit()
        QT_APP.processEvents()

        self.assertIs(
            self.window.stack.currentWidget(), self.window._replay_panel
        )

    def test_album_is_reused_across_visits(self) -> None:
        self.window._open_replay_page()
        QT_APP.processEvents()
        self.window._open_colophon_album()
        QT_APP.processEvents()
        first = self.window._colophon_album

        self.window._colophon_album.closed.emit()
        QT_APP.processEvents()
        self.window._open_colophon_album()
        QT_APP.processEvents()

        self.assertIs(self.window._colophon_album, first)

    # ------------------------------------------------------------------
    # 编辑器
    # ------------------------------------------------------------------
    def test_saving_a_colophon_writes_it_to_the_album(self) -> None:
        from gomoku.client.colophon import make_colophon
        from gomoku.shared.record import GameRecord
        from gomoku.shared.rules import Move, Player

        moves = tuple(
            Move(
                7,
                3 + index % 5,
                Player.BLACK if index % 2 == 0 else Player.WHITE,
            )
            for index in range(6)
        )
        record = GameRecord(
            record_id="rec-1",
            created_at="2026-11-26T14:30:00+00:00",
            mode="local",
            moves=moves,
            winner=None,
            result_reason="",
            black_name="黑棋",
            white_name="白棋",
        )
        colophon = make_colophon(
            record, title="残阳一局", colophon="第三手，定鼎。", ply=3
        )

        self.window._save_colophon(colophon)

        stored = self.window._colophon_store.list_colophons()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].title, "残阳一局")
        self.assertEqual(stored[0].ply, 3)
        self.assertEqual(stored[0].colophon, "第三手，定鼎。")

    def test_last_record_is_cleared_when_a_new_game_starts(self) -> None:
        self.window._last_record = object()  # type: ignore[assignment]
        self.window.start_local_game()
        QT_APP.processEvents()
        self.assertIsNone(self.window._last_record)


if __name__ == "__main__":
    unittest.main(verbosity=2)
