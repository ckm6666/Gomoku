"""字体大小设置测试（离屏）：缩放、持久化与按钮交互。"""

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

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.font_control import FontSizeButton  # noqa: E402
from gomoku.client.font_settings import (  # noqa: E402
    BASE_FONT_POINT_SIZE,
    DEFAULT_FONT_SCALE,
    FONT_SCALE_KEY,
    FONT_SIZE_PRESETS,
    MAX_FONT_SCALE,
    MIN_FONT_SCALE,
    apply_font_scale,
    build_app_font,
    clamp_font_scale,
    current_font_scale,
    load_font_scale,
    preset_label,
    save_font_scale,
    scale_stylesheet,
)
from gomoku.client.styles import APP_STYLE  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


class FontScaleTests(unittest.TestCase):
    """缩放比例的换算与钳制。"""

    def test_clamp_keeps_values_in_range(self) -> None:
        self.assertEqual(clamp_font_scale(0.1), MIN_FONT_SCALE)
        self.assertEqual(clamp_font_scale(9.0), MAX_FONT_SCALE)
        self.assertEqual(clamp_font_scale(1.0), DEFAULT_FONT_SCALE)

    def test_clamp_rejects_invalid_values(self) -> None:
        self.assertEqual(clamp_font_scale("abc"), DEFAULT_FONT_SCALE)
        self.assertEqual(clamp_font_scale(None), DEFAULT_FONT_SCALE)

    def test_preset_label_picks_nearest_preset(self) -> None:
        labels = {scale: label for label, scale in FONT_SIZE_PRESETS}
        self.assertEqual(preset_label(1.0), labels[1.0])
        self.assertEqual(preset_label(1.16), labels[1.15])

    def test_stylesheet_font_sizes_are_scaled(self) -> None:
        scaled = scale_stylesheet(APP_STYLE, 1.3)
        # 31px 的品牌标题放大到 40px（31 * 1.3 = 40.3）。
        self.assertIn("font-size: 40px;", scaled)
        self.assertNotIn("font-size: 31px;", scaled)

    def test_stylesheet_keeps_other_declarations(self) -> None:
        scaled = scale_stylesheet(APP_STYLE, 0.85)
        self.assertIn("color: #26221C;", scaled)
        self.assertIn("QMainWindow {", scaled)

    def test_stylesheet_never_scales_below_minimum(self) -> None:
        scaled = scale_stylesheet(
            "QLabel { font-size: 10px; }", MIN_FONT_SCALE
        )
        self.assertIn("font-size: 8px;", scaled)

    def test_scale_one_returns_original_stylesheet(self) -> None:
        self.assertIs(scale_stylesheet(APP_STYLE, 1.0), APP_STYLE)

    def test_base_font_point_size_follows_scale(self) -> None:
        self.assertEqual(build_app_font(1.0).pointSize(), BASE_FONT_POINT_SIZE)
        self.assertEqual(
            build_app_font(1.3).pointSize(),
            round(BASE_FONT_POINT_SIZE * 1.3),
        )


class FontScalePersistenceTests(unittest.TestCase):
    """QSettings 读写。"""

    @staticmethod
    def _settings(directory: Path) -> QSettings:
        return QSettings(
            str(directory / "font.ini"), QSettings.Format.IniFormat
        )

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            self.assertEqual(load_font_scale(settings), DEFAULT_FONT_SCALE)
            save_font_scale(1.15, settings)
            self.assertEqual(load_font_scale(settings), 1.15)
            self.assertEqual(settings.value(FONT_SCALE_KEY), 1.15)

    def test_invalid_stored_value_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            settings.setValue(FONT_SCALE_KEY, "not-a-number")
            self.assertEqual(load_font_scale(settings), DEFAULT_FONT_SCALE)

    def test_out_of_range_stored_value_is_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            settings.setValue(FONT_SCALE_KEY, 99)
            self.assertEqual(load_font_scale(settings), MAX_FONT_SCALE)


class FontScaleApplyTests(unittest.TestCase):
    """应用到 QApplication 之后的全局效果。"""

    def setUp(self) -> None:
        self._original_font = QT_APP.font()
        self._original_style = QT_APP.styleSheet()

    def tearDown(self) -> None:
        apply_font_scale(QT_APP, DEFAULT_FONT_SCALE)
        QT_APP.setFont(self._original_font)
        QT_APP.setStyleSheet(self._original_style)

    def test_apply_updates_font_and_stylesheet(self) -> None:
        apply_font_scale(QT_APP, 1.3)
        self.assertEqual(current_font_scale(), 1.3)
        self.assertEqual(
            QT_APP.font().pointSize(), round(BASE_FONT_POINT_SIZE * 1.3)
        )
        self.assertIn("font-size: 40px;", QT_APP.styleSheet())

    def test_apply_back_to_default(self) -> None:
        apply_font_scale(QT_APP, 1.3)
        apply_font_scale(QT_APP, 1.0)
        self.assertEqual(current_font_scale(), DEFAULT_FONT_SCALE)
        self.assertIn("font-size: 31px;", QT_APP.styleSheet())


class FontSizeButtonTests(unittest.TestCase):
    """字号按钮：选择档位后立即生效并持久化。"""

    def setUp(self) -> None:
        self._original_font = QT_APP.font()
        self._original_style = QT_APP.styleSheet()
        apply_font_scale(QT_APP, DEFAULT_FONT_SCALE)

    def tearDown(self) -> None:
        apply_font_scale(QT_APP, DEFAULT_FONT_SCALE)
        QT_APP.setFont(self._original_font)
        QT_APP.setStyleSheet(self._original_style)

    def test_menu_lists_all_presets(self) -> None:
        button = FontSizeButton()
        try:
            actions = button.menu().actions()
            self.assertEqual(len(actions), len(FONT_SIZE_PRESETS))
            self.assertEqual(
                [action.text() for action in actions],
                [label for label, _ in FONT_SIZE_PRESETS],
            )
        finally:
            button.deleteLater()

    def test_current_preset_is_checked(self) -> None:
        button = FontSizeButton()
        try:
            apply_font_scale(QT_APP, 1.15)
            button._sync_from_settings()
            checked = [
                action.text()
                for action in button.menu().actions()
                if action.isChecked()
            ]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0], "大")
        finally:
            button.deleteLater()

    def test_selecting_preset_applies_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(
                str(Path(directory) / "font.ini"), QSettings.Format.IniFormat
            )
            button = FontSizeButton(settings=settings)
            try:
                target = dict(FONT_SIZE_PRESETS)["特大"]
                action = next(
                    item
                    for item in button.menu().actions()
                    if item.text() == "特大"
                )
                action.trigger()
                self.assertEqual(current_font_scale(), target)
                self.assertEqual(load_font_scale(settings), target)
                self.assertTrue(action.isChecked())
            finally:
                button.deleteLater()

    def test_other_buttons_sync_after_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(
                str(Path(directory) / "font.ini"), QSettings.Format.IniFormat
            )
            first = FontSizeButton(settings=settings)
            second = FontSizeButton(settings=settings)
            try:
                target = dict(FONT_SIZE_PRESETS)["大"]
                action = next(
                    item
                    for item in first.menu().actions()
                    if item.text() == "大"
                )
                action.trigger()
                checked = [
                    item.text()
                    for item in second.menu().actions()
                    if item.isChecked()
                ]
                self.assertEqual(checked, ["大"])
                self.assertEqual(current_font_scale(), target)
            finally:
                first.deleteLater()
                second.deleteLater()


if __name__ == "__main__":
    unittest.main()
