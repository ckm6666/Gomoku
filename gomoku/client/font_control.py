"""字体大小按钮：主菜单、对局页与联机页共用的“字号”下拉按钮。

按钮本身是应用级控件：菜单里选择小/标准/大/特大后，立即改写全局字号并
写入 QSettings；所有页面共享同一份比例，任何入口改动都会立刻生效。
"""

from __future__ import annotations

import weakref
from typing import Optional

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QToolButton,
    QWidget,
)

from gomoku.client.font_settings import (
    FONT_SIZE_PRESETS,
    apply_font_scale,
    current_font_scale,
    preset_label,
    save_font_scale,
)
from gomoku.client.ui_theme import make_font_icon

#: 存活的按钮实例：字号变化后统一刷新勾选状态与提示文字。
_INSTANCES: "weakref.WeakSet[FontSizeButton]" = weakref.WeakSet()


class FontSizeButton(QToolButton):
    """一个紧凑的“字体大小”下拉按钮。

    菜单列出四档字号，当前档位带勾选标记；选中后立即应用到整个应用。
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        settings: Optional[QSettings] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._syncing = False
        _INSTANCES.add(self)

        self.setObjectName("fontSizeButton")
        self.setAutoRaise(True)
        self.setIconSize(QSize(18, 18))
        self.setFixedSize(34, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.setAccessibleName("字体大小")

        self._menu = QMenu(self)
        self._menu.setObjectName("fontMenu")

        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        self._actions: dict[str, QAction] = {}

        for label, scale in FONT_SIZE_PRESETS:
            action = QAction(label, self._menu)
            action.setCheckable(True)
            action.setData(scale)
            action.setToolTip(f"界面字号：{label}")
            self._group.addAction(action)
            self._menu.addAction(action)
            self._actions[label] = action
            action.triggered.connect(
                lambda _checked=False, value=scale: self._on_scale_selected(value)
            )

        self.setMenu(self._menu)
        self._menu.aboutToShow.connect(self._sync_from_settings)
        self._sync_from_settings()

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_scale_selected(self, scale: float) -> None:
        """应用并持久化选中的字号比例。"""
        if self._syncing:
            return
        save_font_scale(scale, self._settings)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_font_scale(app, scale)
        # 其他页面的按钮也要同步显示当前档位。
        for button in list(_INSTANCES):
            button._sync_from_settings()

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------
    def _sync_from_settings(self) -> None:
        """按当前生效的比例更新勾选状态与提示文字。"""
        if self._syncing:
            return
        self._syncing = True
        try:
            scale = current_font_scale()
            label = preset_label(scale)
            for name, action in self._actions.items():
                action.setChecked(name == label)
            self.setToolTip(f"字体大小：{label}")
            self.setIcon(
                QIcon(
                    make_font_icon(
                        18,
                        dpr=max(1.0, self.devicePixelRatioF()),
                    )
                )
            )
        finally:
            self._syncing = False


__all__ = ["FontSizeButton"]
