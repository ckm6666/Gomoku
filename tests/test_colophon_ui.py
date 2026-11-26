"""题跋拓片的界面接线测试（离屏）。

覆盖三处接线：终局弹窗的“拓片”入口、复盘页的“拓此手/拓片册”按钮，
以及编辑器与册页本身的行为。接线是最容易出错的地方——模型对了，
按钮没连上也白搭。
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

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from gomoku.client.colophon import (  # noqa: E402
    MAX_TITLE_LENGTH,
    Colophon,
    ColophonStore,
    make_colophon,
)
from gomoku.client.colophon_album import ColophonAlbum  # noqa: E402
from gomoku.client.colophon_card import (  # noqa: E402
    CARD_WIDTH,
    ColophonCard,
    render_pixmap,
)
from gomoku.client.colophon_editor import ColophonEditor  # noqa: E402
from gomoku.client.replay_panel import ReplayPanel  # noqa: E402
from gomoku.client.result_dialog import (  # noqa: E402
    ResultDialog,
    ResultPresentation,
)
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402
from gomoku.shared.record import GameRecord  # noqa: E402
from gomoku.shared.rules import Move, Player  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])
apply_app_theme(QT_APP)


def _moves() -> tuple[Move, ...]:
    """黑棋在第 9 手连成五子的一局。

    终局之后不能再有落子，因此这局正好 9 手：黑棋第 5 手成五，
    白棋只下了 4 手。
    """
    black = [(7, 2), (7, 3), (7, 4), (7, 5), (7, 6)]
    white = [(9, 9), (10, 9), (11, 9), (12, 9)]
    sequence: list[tuple[int, int]] = []
    for index in range(5):
        sequence.append(black[index])
        if index < len(white):
            sequence.append(white[index])
    return tuple(
        Move(
            row,
            col,
            Player.BLACK if index % 2 == 0 else Player.WHITE,
        )
        for index, (row, col) in enumerate(sequence)
    )


def _record(record_id: str = "rec-1") -> GameRecord:
    return GameRecord(
        record_id=record_id,
        created_at="2026-11-26T14:30:00+00:00",
        mode="ai",
        moves=_moves(),
        winner=Player.BLACK,
        result_reason="five",
        black_name="墨客",
        white_name="松风",
    )


class ResultDialogColophonTests(unittest.TestCase):
    """终局弹窗的“拓片”入口。"""

    def _presentation(self) -> ResultPresentation:
        return ResultPresentation(
            kind="win",
            title="胜利",
            subtitle="执黑胜",
            detail="第 9 手",
            seal_text="胜",
        )

    def test_colophon_button_is_visible_by_default(self) -> None:
        dialog = ResultDialog(self._presentation())
        self.assertTrue(dialog.colophon_button.isVisibleTo(dialog))
        self.assertFalse(dialog.colophon_requested)

    def test_colophon_button_can_be_disabled(self) -> None:
        dialog = ResultDialog(
            self._presentation(), colophon_enabled=False
        )
        self.assertFalse(dialog.colophon_button.isVisibleTo(dialog))

    def test_clicking_colophon_records_the_intent(self) -> None:
        dialog = ResultDialog(self._presentation())
        dialog.colophon_button.click()
        self.assertTrue(dialog.colophon_requested)
        # 拓片不是“再来一局”：接受语义不能被顶掉。
        self.assertFalse(dialog.restart_requested)


class ReplayPanelColophonTests(unittest.TestCase):
    """复盘页的“拓此手”与“拓片册”。"""

    def setUp(self) -> None:
        self._panel = ReplayPanel([_record()])
        self._panel.show()

    def tearDown(self) -> None:
        self._panel.shutdown()
        self._panel.deleteLater()

    def test_buttons_exist(self) -> None:
        self.assertTrue(hasattr(self._panel, "colophon_button"))
        self.assertTrue(hasattr(self._panel, "album_button"))

    def test_colophon_is_disabled_without_a_move(self) -> None:
        self._panel._set_index(0)
        self.assertFalse(self._panel.colophon_button.isEnabled())

    def test_colophon_is_enabled_from_the_first_move(self) -> None:
        self._panel._set_index(1)
        self.assertTrue(self._panel.colophon_button.isEnabled())

    def test_clicking_emits_the_current_ply(self) -> None:
        received: list[int] = []
        self._panel.colophon_requested.connect(received.append)
        self._panel._set_index(4)
        self._panel.colophon_button.click()
        self.assertEqual(received, [4])

    def test_album_button_emits(self) -> None:
        received: list[bool] = []
        self._panel.album_requested.connect(lambda: received.append(True))
        self._panel.album_button.click()
        self.assertEqual(received, [True])

    def test_current_record_is_exposed(self) -> None:
        self.assertIsNotNone(self._panel.current_record())
        self.assertEqual(self._panel.current_record().record_id, "rec-1")


class ColophonEditorTests(unittest.TestCase):
    """编辑器：选手、拟题、预览与保存。"""

    def setUp(self) -> None:
        self._host = QWidget()
        self._host.resize(720, 900)
        self._editor = ColophonEditor(_record(), parent=self._host, ply=9)

    def tearDown(self) -> None:
        self._editor.deleteLater()
        self._host.deleteLater()

    def test_defaults_to_the_given_ply(self) -> None:
        self.assertEqual(self._editor.ply_label.text(), "第 9 / 9 手")

    def test_stepping_the_ply_rebuilds_the_preview(self) -> None:
        self._editor._step_ply(-1)
        self.assertEqual(self._editor.ply_label.text(), "第 8 / 9 手")
        self.assertEqual(self._editor.preview.colophon().ply, 8)

    def test_ply_cannot_run_past_the_record(self) -> None:
        for _ in range(20):
            self._editor._step_ply(1)
        self.assertEqual(self._editor.ply_label.text(), "第 9 / 9 手")
        self.assertFalse(self._editor.next_ply_button.isEnabled())

    def test_title_and_text_update_the_preview(self) -> None:
        self._editor.title_input.setText("残阳一局")
        self._editor.text_edit.setPlainText("第九手落定。")
        preview = self._editor.preview.colophon()
        self.assertEqual(preview.title, "残阳一局")
        self.assertEqual(preview.colophon, "第九手落定。")

    def test_text_is_truncated_to_the_limit(self) -> None:
        self._editor.text_edit.setPlainText("长" * 200)
        self.assertLessEqual(
            len(self._editor.text_edit.toPlainText()), 60
        )

    def test_draft_fills_the_text_edit(self) -> None:
        # 下拉框里选中不等于已经写进正文，得按「拟题」才套用。
        self._editor.voice_combo.setCurrentIndex(1)
        self.assertEqual(self._editor.text_edit.toPlainText().strip(), "")
        self._editor.draft_button.click()
        self.assertTrue(self._editor.text_edit.toPlainText().strip())
        self.assertIn("第 9 手", self._editor.text_edit.toPlainText())

    def test_saving_emits_a_colophon(self) -> None:
        received: list[Colophon] = []
        self._editor.saved.connect(received.append)
        self._editor.title_input.setText("残阳一局")
        self._editor.text_edit.setPlainText("第九手落定。")
        self._editor._on_save()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].title, "残阳一局")
        self.assertEqual(received[0].ply, 9)

    def test_saving_without_a_title_does_not_emit(self) -> None:
        received: list[Colophon] = []
        self._editor.saved.connect(received.append)
        self._editor.title_input.setText("   ")
        self._editor._on_save()
        self.assertEqual(received, [])

    def test_a_title_is_prefilled(self) -> None:
        """编辑器一打开就该有题名。

        空题名既刻不出引首印，存册时也会被挡回来——玩家多半只是改
        两三个字，而不是从空白里憋一个。
        """
        title = self._editor.title_input.text().strip()
        self.assertTrue(title)
        self.assertLessEqual(len(title), MAX_TITLE_LENGTH)
        self.assertEqual(self._editor.preview.colophon().title, title)

    def test_enter_in_the_title_field_saves(self) -> None:
        """题名框里按回车要能存册。

        焦点一落到输入框，遮罩就收不到回车键了；不回车只能去点
        「存册」，是玩家最容易踩空的一步。
        """
        received: list[Colophon] = []
        self._editor.saved.connect(received.append)
        self._editor.title_input.setText("残阳一局")
        self._editor.text_edit.setPlainText("第九手落定。")
        self._editor.title_input.returnPressed.emit()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].title, "残阳一局")


class ColophonCardTests(unittest.TestCase):
    """卡片渲染。"""

    def test_card_renders_a_pixmap(self) -> None:
        colophon = make_colophon(
            _record(), title="残阳一局", colophon="第九手落定。", ply=9
        )
        card = ColophonCard(colophon)
        pixmap = card.to_pixmap()
        self.assertFalse(pixmap.isNull())
        self.assertGreater(pixmap.width(), 0)
        self.assertGreater(pixmap.height(), 0)

    def test_card_uses_the_focus_stone(self) -> None:
        colophon = make_colophon(
            _record(), title="残阳一局", colophon="第九手落定。", ply=9
        )
        self.assertEqual(colophon.focus(), (7, 6))
        self.assertEqual(colophon.focus_side(), Player.BLACK)


class ColophonAlbumTests(unittest.TestCase):
    """册页：网格、选中与空册。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._store = ColophonStore(Path(self._tmpdir.name) / "colophons")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_empty_album(self) -> None:
        album = ColophonAlbum(self._store)
        self.assertEqual(album.count_label.text(), "还没有拓片")

    def test_album_lists_entries(self) -> None:
        self._store.add(
            make_colophon(
                _record("a"), title="残阳一局", colophon="甲。", ply=9
            )
        )
        self._store.add(
            make_colophon(
                _record("b"), title="松风残局", colophon="乙。", ply=7
            )
        )
        album = ColophonAlbum(self._store)
        self.assertEqual(album.count_label.text(), "共 2 幅拓片")

    def test_refresh_picks_up_new_entries(self) -> None:
        album = ColophonAlbum(self._store)
        self.assertEqual(album.count_label.text(), "还没有拓片")
        self._store.add(
            make_colophon(
                _record("a"), title="残阳一局", colophon="甲。", ply=9
            )
        )
        album.refresh()
        self.assertEqual(album.count_label.text(), "共 1 幅拓片")

    def test_actions_are_disabled_without_a_selection(self) -> None:
        album = ColophonAlbum(self._store)
        self.assertFalse(album.export_png_button.isEnabled())
        self.assertFalse(album.delete_button.isEnabled())

    def test_selecting_enables_the_actions(self) -> None:
        colophon = make_colophon(
            _record("a"), title="残阳一局", colophon="甲。", ply=9
        )
        self._store.add(colophon)
        album = ColophonAlbum(self._store)
        album._select(colophon.id)
        self.assertTrue(album.export_png_button.isEnabled())
        self.assertTrue(album.delete_button.isEnabled())

    def test_a_corrupt_album_says_so(self) -> None:
        """册子坏了要说出来，不能显示成"还没有拓片"。

        后者会让玩家以为自己的收藏凭空没了。
        """
        self._store.directory.mkdir(parents=True, exist_ok=True)
        self._store.path.write_text("{ 坏掉的册子", encoding="utf-8")
        album = ColophonAlbum(self._store)
        self.assertIn("册子读取失败", album.status_label.text())

    def test_an_empty_album_does_not_say_it_is_broken(self) -> None:
        album = ColophonAlbum(self._store)
        self.assertNotIn("册子读取失败", album.status_label.text())


class ColophonRenderTests(unittest.TestCase):
    """离屏渲染。"""

    def test_export_uses_the_full_card_size(self) -> None:
        """导出的 PNG 按整幅 340 宽渲染，不跟着网格里的缩略尺寸缩水。"""
        colophon = make_colophon(
            _record(), title="残阳一局", colophon="第九手落定。", ply=9
        )
        pixmap = render_pixmap(colophon)
        self.assertEqual(pixmap.width(), CARD_WIDTH)
        self.assertGreater(pixmap.height(), 0)

    def test_an_empty_title_paints_no_seal(self) -> None:
        """题名为空时不画引首印。

        印面是朱砂底，没有字的时候画出来就是一个空红方块，看着像刻
        坏了的印。
        """
        colophon = make_colophon(
            _record(), title="", colophon="第九手落定。", ply=9
        )
        pixmap = render_pixmap(colophon)
        image = pixmap.toImage()
        # 引首印在卡片右上角：只在这一小块里找朱砂色像素。
        cinnabar = 0
        for y in range(0, 70):
            for x in range(pixmap.width() - 70, pixmap.width()):
                color = image.pixelColor(x, y)
                if (
                    color.red() > 150
                    and color.green() < 90
                    and color.blue() < 90
                ):
                    cinnabar += 1
        self.assertEqual(cinnabar, 0)

    def test_a_title_paints_a_seal(self) -> None:
        colophon = make_colophon(
            _record(), title="残阳一局", colophon="第九手落定。", ply=9
        )
        pixmap = render_pixmap(colophon)
        image = pixmap.toImage()
        cinnabar = 0
        for y in range(0, 70):
            for x in range(pixmap.width() - 70, pixmap.width()):
                color = image.pixelColor(x, y)
                if (
                    color.red() > 150
                    and color.green() < 90
                    and color.blue() < 90
                ):
                    cinnabar += 1
        self.assertGreater(cinnabar, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
