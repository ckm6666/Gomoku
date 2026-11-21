"""古风主题资源、绘制与终局弹窗测试（离屏）。"""

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

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from gomoku.client import theme_palette  # noqa: E402
from gomoku.client.board_view import BoardView  # noqa: E402
from gomoku.client.board_widget import BoardWidget  # noqa: E402
from gomoku.client.confirm_dialog import ConfirmDialog  # noqa: E402
from gomoku.client.result_dialog import (  # noqa: E402
    ResultDialog,
    ResultPresentation,
)
from gomoku.client.styles import APP_STYLE  # noqa: E402
from gomoku.client.ui_theme import (  # noqa: E402
    ASSET_BAMBOO,
    ASSET_GAME_SCROLL,
    ASSET_MENU_BACKDROP,
    ASSET_ONLINE_BACKDROP,
    ASSET_PLUM,
    ASSET_RESULT_BACKDROP,
    BackdropSpec,
    InkBackdrop,
    MENU_BACKDROP_SPEC,
    RicePaperCard,
    apply_app_theme,
    assets_root,
    find_font_asset,
    find_ui_asset,
    fonts_directory,
    load_theme_fonts,
    make_ink_divider,
    make_seal_pixmap,
    render_backdrop,
    ui_assets_directory,
)
from gomoku.shared.game_model import GameModel  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])

_UI_ASSETS = (
    ASSET_MENU_BACKDROP,
    ASSET_GAME_SCROLL,
    ASSET_RESULT_BACKDROP,
    ASSET_PLUM,
    ASSET_BAMBOO,
    ASSET_ONLINE_BACKDROP,
)


class ThemeResourceTests(unittest.TestCase):
    """资源路径、字体与主题应用。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")
        cls.families = apply_app_theme(QT_APP)

    def test_ui_assets_exist(self) -> None:
        for name in _UI_ASSETS:
            with self.subTest(name=name):
                self.assertIsNotNone(find_ui_asset(name))
                self.assertTrue((ui_assets_directory() / name).is_file())

    def test_fonts_exist_and_load(self) -> None:
        self.assertIsNotNone(
            find_font_asset("MaShanZheng-Regular.ttf")
        )
        self.assertIsNotNone(
            find_font_asset("ZCOOLXiaoWei-Regular.ttf")
        )
        self.assertTrue(
            (fonts_directory() / "OFL-MaShanZheng.txt").is_file()
        )
        self.assertTrue(
            (fonts_directory() / "OFL-ZCOOLXiaoWei.txt").is_file()
        )
        families = load_theme_fonts()
        self.assertIn("Ma Shan Zheng", families)
        self.assertIn("ZCOOL XiaoWei", families)

    def test_apply_theme_sets_font_and_qss(self) -> None:
        apply_app_theme(QT_APP)
        self.assertEqual(QT_APP.styleSheet(), APP_STYLE)
        families = QT_APP.font().families()
        # 正文优先使用清晰度更好的仿宋/楷体，ZCOOL 作为内置 CJK 回退。
        self.assertIn("FangSong", families)
        self.assertIn("ZCOOL XiaoWei", families)
        self.assertIn(theme_palette.CINNABAR, APP_STYLE)
        self.assertIn(theme_palette.PAPER, APP_STYLE)

    def test_frozen_asset_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = (
                Path(directory)
                / "gomoku"
                / "client"
                / "assets"
                / "ui"
                / ASSET_MENU_BACKDROP
            )
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"fake")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(
                        sys, "_MEIPASS", directory, create=True
                    ):
                self.assertEqual(
                    assets_root(),
                    Path(directory) / "gomoku" / "client" / "assets",
                )
                self.assertEqual(find_ui_asset(ASSET_MENU_BACKDROP), asset)


class ThemePaintingTests(unittest.TestCase):
    """印章、纸纹、背景、卡片与棋盘绘制。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")
        apply_app_theme(QT_APP)

    def test_seal_and_divider(self) -> None:
        seal = make_seal_pixmap("弈", 48, dpr=2.0)
        self.assertFalse(seal.isNull())
        self.assertEqual(seal.width(), 96)
        self.assertEqual(seal.height(), 96)
        divider = make_ink_divider(200, 16, dpr=2.0)
        self.assertFalse(divider.isNull())
        self.assertEqual(divider.width(), 400)

    def test_backdrop_renders(self) -> None:
        pixmap = render_backdrop(QSize(320, 240), 1.0, MENU_BACKDROP_SPEC)
        self.assertFalse(pixmap.isNull())
        self.assertEqual(pixmap.size(), QSize(320, 240))

    def test_missing_asset_falls_back_to_paper(self) -> None:
        widget = InkBackdrop(
            spec=BackdropSpec(image="definitely-missing.jpg")
        )
        widget.resize(320, 240)
        widget.show()
        QT_APP.processEvents()
        pixmap = widget.grab()
        self.assertFalse(pixmap.isNull())
        self.assertGreater(
            pixmap.toImage().pixelColor(20, 20).alpha(), 0
        )
        widget.close()

    def test_rice_paper_card_renders(self) -> None:
        card = RicePaperCard(seal_text="弈")
        card.resize(360, 420)
        card.show()
        QT_APP.processEvents()
        self.assertFalse(card.grab().isNull())
        card.close()

    def test_board_hit_mapping_roundtrip(self) -> None:
        board = BoardWidget(BoardView.from_model(GameModel()))
        board.resize(500, 500)
        board.show()
        QT_APP.processEvents()
        board.grab()
        for row, col in ((0, 0), (7, 7), (14, 14), (0, 14), (14, 0)):
            with self.subTest(row=row, col=col):
                self.assertEqual(
                    board._cell_at(board._point_for(row, col)),
                    (row, col),
                )
        board.resize(360, 360)
        QT_APP.processEvents()
        board.grab()
        self.assertEqual(
            board._cell_at(board._point_for(7, 7)), (7, 7)
        )
        board.close()


class ResultDialogTests(unittest.TestCase):
    """终局弹窗三种状态与按钮语义。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")
        apply_app_theme(QT_APP)

    @staticmethod
    def _presentation(kind: str) -> ResultPresentation:
        if kind == "win":
            return ResultPresentation(
                "win", "胜利", "你获胜 · AI（中等）", "本局共 9 手", "胜"
            )
        if kind == "lose":
            return ResultPresentation(
                "lose", "惜败", "AI（中等）获胜", "本局共 9 手", "负"
            )
        return ResultPresentation(
            "draw", "和棋", "棋盘已满，胜负未分", "本局共 225 手", "和"
        )

    def test_variants_construct_with_expected_text(self) -> None:
        for kind in ("win", "lose", "draw"):
            with self.subTest(kind=kind):
                dialog = ResultDialog(self._presentation(kind))
                self.assertTrue(dialog.isModal())
                self.assertEqual(
                    dialog.title_label.text(),
                    self._presentation(kind).title,
                )
                self.assertEqual(dialog.restart_button.text(), "再来一局")
                self.assertEqual(dialog.view_button.text(), "查看棋盘")
                dialog.deleteLater()

    def test_restart_button_accepts(self) -> None:
        dialog = ResultDialog(self._presentation("win"))
        dialog.restart_button.click()
        self.assertEqual(
            dialog.result(), QDialog.DialogCode.Accepted
        )
        self.assertTrue(dialog.restart_requested)

    def test_view_button_rejects(self) -> None:
        dialog = ResultDialog(self._presentation("lose"))
        dialog.view_button.click()
        self.assertEqual(
            dialog.result(), QDialog.DialogCode.Rejected
        )
        self.assertFalse(dialog.restart_requested)


class ConfirmDialogTests(unittest.TestCase):
    """古风确认弹窗的文案与确认/取消语义。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")
        apply_app_theme(QT_APP)

    def test_confirm_dialog_text_and_accept(self) -> None:
        dialog = ConfirmDialog(
            "重新开局",
            "当前棋局将被清空，确定要重新开局吗？",
            confirm_text="开始新对局",
        )
        self.assertTrue(dialog.isModal())
        self.assertEqual(dialog.title_label.text(), "重新开局")
        self.assertIn("清空", dialog.message_label.text())
        self.assertEqual(dialog.confirm_button.text(), "开始新对局")
        self.assertEqual(dialog.cancel_button.text(), "取消")
        self.assertTrue(dialog.cancel_button.isDefault())
        dialog.confirm_button.click()
        self.assertEqual(
            dialog.result(), QDialog.DialogCode.Accepted
        )
        self.assertTrue(dialog.confirm_requested)

    def test_confirm_dialog_cancel(self) -> None:
        dialog = ConfirmDialog(
            "返回主菜单",
            "返回主菜单将结束当前棋局，确定要返回吗？",
            confirm_text="返回主菜单",
        )
        dialog.cancel_button.click()
        self.assertEqual(
            dialog.result(), QDialog.DialogCode.Rejected
        )
        self.assertFalse(dialog.confirm_requested)


if __name__ == "__main__":
    unittest.main(verbosity=2)
