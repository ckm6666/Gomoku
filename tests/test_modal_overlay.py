"""模态遮罩层的绘制契约测试（离屏）。

遮罩是半透明墨色，必须让宿主的内容透出来。这里锁住一个容易复发的坑：
``WA_OpaquePaintEvent`` 的语义是"我保证画满每一像素，Qt 不用重画我下面
的东西"，而遮罩恰恰只画一层半透明墨色。带着这个属性时，遮罩每次重绘
都不会让宿主重画，新的墨色就叠在旧的墨色上——点几下弹窗，背景越点越黑。
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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from gomoku.client.confirm_dialog import ConfirmDialog  # noqa: E402
from gomoku.client.modal_overlay import (  # noqa: E402
    ModalOverlay,
    NoticeOverlay,
    PaperCard,
)
from gomoku.client.result_dialog import (  # noqa: E402
    ResultDialog,
    ResultPresentation,
)

QT_APP = QApplication.instance() or QApplication([])


class ModalOverlayPaintContractTests(unittest.TestCase):
    """遮罩的绘制契约。"""

    def setUp(self) -> None:
        self._host = QWidget()
        self._host.resize(400, 300)
        self._overlay = ModalOverlay(self._host)

    def tearDown(self) -> None:
        self._overlay.deleteLater()
        self._host.deleteLater()

    def test_overlay_is_not_opaque_paint_event(self) -> None:
        """遮罩不能声明"我画满每一像素"。

        它只画一层半透明墨色；声明了 opaque 就会让 Qt 跳过宿主重画，
        半透明墨色于是层层叠加。
        """
        self.assertFalse(
            self._overlay.testAttribute(
                Qt.WidgetAttribute.WA_OpaquePaintEvent
            )
        )

    def test_overlay_background_is_translucent(self) -> None:
        """遮罩的背景必须透明，宿主的内容才透得出来。"""
        self.assertTrue(
            self._overlay.testAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground
            )
        )
        self.assertTrue(
            self._overlay.testAttribute(
                Qt.WidgetAttribute.WA_NoSystemBackground
            )
        )
        self.assertFalse(self._overlay.autoFillBackground())

    def test_the_card_inside_may_still_be_opaque(self) -> None:
        """卡片本身画的是不透明的宣纸底，可以照常声明 opaque。"""
        card = PaperCard(self._overlay)
        self.assertTrue(
            card.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        )
        card.deleteLater()


class OverlaySubclassesTests(unittest.TestCase):
    """各类弹窗都继承同一套遮罩契约。"""

    def setUp(self) -> None:
        self._host = QWidget()
        self._host.resize(400, 300)

    def tearDown(self) -> None:
        self._host.deleteLater()

    def _assert_contract(self, overlay: ModalOverlay) -> None:
        self.assertFalse(
            overlay.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        )
        self.assertTrue(
            overlay.testAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground
            )
        )
        overlay.deleteLater()

    def test_notice_overlay(self) -> None:
        self._assert_contract(NoticeOverlay("标题", "消息", parent=self._host))

    def test_confirm_dialog(self) -> None:
        self._assert_contract(ConfirmDialog("标题", "消息", parent=self._host))

    def test_result_dialog(self) -> None:
        presentation = ResultPresentation(
            kind="win",
            title="胜利",
            subtitle="执黑胜",
            detail="第 9 手",
            seal_text="胜",
        )
        self._assert_contract(
            ResultDialog(presentation, self._host)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
