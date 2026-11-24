"""全局字体大小设置：统一缩放 QSS 字号与基础字号，并持久化到 QSettings。

界面字号分散在三处：应用级 QSS（px）、应用基础字号（point size）、少数自绘
控件直接设置的字号。本模块把「字体大小」抽象成一个缩放比例：

* QSS 里的 px 字号按比例改写，颜色、间距与边框保持不变，避免界面被整体
  放大后走样；
* 应用基础字号按比例取整，覆盖没有单独写 ``font-size`` 的控件（菜单项、
  列表项、下拉框等）；
* 自绘控件通过 :func:`current_font_scale` 读取当前比例。

比例保存在 QSettings（``ui/fontScale``），跨重启生效；主菜单、对局页与联机页
共用同一份比例，任何入口改动都会立刻作用到整个应用。
"""

from __future__ import annotations

import logging
import re
from typing import Final, Optional

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from gomoku.client.styles import APP_STYLE

logger = logging.getLogger(__name__)

#: QSettings 键名。
FONT_SCALE_KEY: Final[str] = "ui/fontScale"
#: 应用基础字号（点）：未缩放时与 QSS 的 px 字号视觉匹配。
BASE_FONT_POINT_SIZE: Final[int] = 10
#: 缩放比例的默认值与上下限，避免把界面缩到不可用。
DEFAULT_FONT_SCALE: Final[float] = 1.0
MIN_FONT_SCALE: Final[float] = 0.8
MAX_FONT_SCALE: Final[float] = 1.6
#: 缩放后的最小字号（px），再小就难以辨认。
MIN_FONT_PX: Final[int] = 8

#: 字号档位：显示名 -> 缩放比例。
FONT_SIZE_PRESETS: Final[tuple[tuple[str, float], ...]] = (
    ("小", 0.85),
    ("标准", 1.0),
    ("大", 1.15),
    ("特大", 1.3),
)

#: 正文字体回退链：仿宋/楷体在古风界面里更清晰，项目内置字体兜底。
BODY_FONT_FAMILIES: Final[tuple[str, ...]] = (
    "FangSong",
    "KaiTi",
    "Microsoft YaHei UI",
    "ZCOOL XiaoWei",
    "Microsoft YaHei",
    "Segoe UI",
)

_FONT_SIZE_RE = re.compile(r"(font-size:\s*)(\d+(?:\.\d+)?)(px)")

#: 当前生效的缩放比例，供自绘控件读取。
_current_scale: float = DEFAULT_FONT_SCALE


def clamp_font_scale(scale: object) -> float:
    """把缩放比例钳制到可用区间；非法值回落到默认值。"""
    try:
        value = float(scale)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("字号比例无效，回退默认值：%r", scale)
        return DEFAULT_FONT_SCALE
    if value != value:  # NaN
        logger.warning("字号比例无效，回退默认值：%r", scale)
        return DEFAULT_FONT_SCALE
    return max(MIN_FONT_SCALE, min(MAX_FONT_SCALE, round(value, 2)))


def preset_label(scale: float) -> str:
    """返回最接近当前比例的档位名称。"""
    value = clamp_font_scale(scale)
    label, _ = min(
        FONT_SIZE_PRESETS, key=lambda item: abs(item[1] - value)
    )
    return label


def scale_stylesheet(qss: str, scale: float) -> str:
    """按比例改写 QSS 里的 px 字号，其余声明原样保留。"""
    factor = clamp_font_scale(scale)
    if abs(factor - DEFAULT_FONT_SCALE) < 1e-6:
        return qss

    def _replace(match: "re.Match[str]") -> str:
        size = max(MIN_FONT_PX, int(round(float(match.group(2)) * factor)))
        return f"{match.group(1)}{size}px"

    return _FONT_SIZE_RE.sub(_replace, qss)


def build_app_font(scale: float = DEFAULT_FONT_SCALE) -> QFont:
    """构造应用基础字体：字体回退链固定，字号按比例缩放。"""
    font = QFont()
    font.setFamilies(list(BODY_FONT_FAMILIES))
    point_size = max(
        1, int(round(BASE_FONT_POINT_SIZE * clamp_font_scale(scale)))
    )
    font.setPointSize(point_size)
    return font


def current_font_scale() -> float:
    """返回当前生效的缩放比例。"""
    return _current_scale


def load_font_scale(settings: Optional[QSettings] = None) -> float:
    """读取已保存的缩放比例；未设置或损坏时回落到默认值。"""
    store = settings if settings is not None else QSettings()
    raw = store.value(FONT_SCALE_KEY, DEFAULT_FONT_SCALE)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("字号设置无效，回退默认值：%r", raw)
        return DEFAULT_FONT_SCALE
    return clamp_font_scale(value)


def save_font_scale(
    scale: float, settings: Optional[QSettings] = None
) -> float:
    """保存缩放比例，返回实际保存的值。"""
    value = clamp_font_scale(scale)
    store = settings if settings is not None else QSettings()
    store.setValue(FONT_SCALE_KEY, value)
    store.sync()
    return value


def apply_font_scale(app: QApplication, scale: float) -> float:
    """把缩放比例应用到整个应用（基础字号 + QSS），返回实际生效的比例。"""
    global _current_scale

    factor = clamp_font_scale(scale)
    app.setFont(build_app_font(factor))
    app.setStyleSheet(scale_stylesheet(APP_STYLE, factor))
    _current_scale = factor
    logger.info("应用字号比例：%s（%s）", factor, preset_label(factor))
    return factor


def reset_for_tests() -> None:
    """把进程内缓存的字号比例恢复为默认值；仅供测试隔离使用。"""
    global _current_scale
    _current_scale = DEFAULT_FONT_SCALE


__all__ = [
    "BASE_FONT_POINT_SIZE",
    "BODY_FONT_FAMILIES",
    "DEFAULT_FONT_SCALE",
    "FONT_SCALE_KEY",
    "FONT_SIZE_PRESETS",
    "MAX_FONT_SCALE",
    "MIN_FONT_SCALE",
    "MIN_FONT_PX",
    "apply_font_scale",
    "build_app_font",
    "clamp_font_scale",
    "current_font_scale",
    "load_font_scale",
    "preset_label",
    "reset_for_tests",
    "save_font_scale",
    "scale_stylesheet",
]
