"""主题组合框测试（离屏）：弹窗黑边与自绘下拉箭头。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from gomoku.client.styles import APP_STYLE  # noqa: E402
from gomoku.client.ui_theme import PaperComboBox  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


def _luminance(color) -> float:
    """返回像素的感知亮度。"""
    return (
        0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    )


class PaperComboBoxTests(unittest.TestCase):
    """自绘组合框的两项回归：弹窗黑边、下拉箭头。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 与真实应用一致：Fusion 风格 + 全局 QSS。
        cls._previous_style = QT_APP.style().objectName()
        cls._previous_qss = QT_APP.styleSheet()
        QT_APP.setStyle("Fusion")
        QT_APP.setStyleSheet(APP_STYLE)

    @classmethod
    def tearDownClass(cls) -> None:
        QT_APP.setStyleSheet(cls._previous_qss)
        if cls._previous_style:
            QT_APP.setStyle(cls._previous_style)

    def _make_combo(self, cls=QComboBox) -> QComboBox:
        combo = cls()
        combo.addItems(["每手 15 秒", "每手 30 秒", "每手 60 秒"])
        combo.resize(160, 28)
        combo.show()
        QT_APP.processEvents()
        self.addCleanup(combo.close)
        return combo

    def test_popup_has_no_native_dark_frame(self) -> None:
        """下拉列表外围不得出现系统原生的边框。

        原生边框由 Qt 内部的弹窗容器绘制，应用级 QSS 命中不到它，会在米色
        列表外面露出一圈中性灰（Windows 上是近黑的 ``#131313``）的边框。
        主题色都是暖色（红通道明显高于蓝通道），原生边框是中性灰，据此
        判断是否泄漏。
        """
        combo = self._make_combo(PaperComboBox)
        combo.showPopup()
        QT_APP.processEvents()
        try:
            view = combo.view()
            container = view.parentWidget()
            self.assertIsNotNone(container)
            image = container.grab().toImage()
            ratio = container.devicePixelRatioF()
            view_rect = view.geometry()

            leaked = []
            for y in range(image.height()):
                for x in range(image.width()):
                    # 只看视图之外的边框区域。
                    if view_rect.contains(x / ratio, y / ratio):
                        continue
                    color = image.pixelColor(x, y)
                    spread = max(
                        color.red(), color.green(), color.blue()
                    ) - min(color.red(), color.green(), color.blue())
                    if spread <= 4:
                        leaked.append((x, y, color.name()))
            self.assertEqual(leaked, [], f"弹窗仍露出系统边框：{leaked[:8]}")
        finally:
            combo.hidePopup()
            QT_APP.processEvents()

    def test_combo_paints_its_own_arrow(self) -> None:
        """自绘箭头必须真的画出来。

        应用 QSS 样式化了 ``QComboBox::drop-down`` 之后，Qt 不再画原生箭头，
        原生组合框右侧只剩一条分隔线；主题组合框要自己补一个 V 形箭头。
        """
        plain = self._make_combo(QComboBox)
        paper = self._make_combo(PaperComboBox)

        def arrow_pixels(image) -> int:
            # 排除控件上下描边、右侧圆角，以及 drop-down 分隔线。
            width = image.width()
            height = image.height()
            region = image.copy(width - 18, 3, 13, height - 6)
            count = 0
            for y in range(region.height()):
                for x in range(region.width()):
                    if _luminance(region.pixelColor(x, y)) < 200:
                        count += 1
            return count

        self.assertEqual(arrow_pixels(plain.grab().toImage()), 0)
        self.assertGreater(arrow_pixels(paper.grab().toImage()), 0)


if __name__ == "__main__":
    unittest.main()
