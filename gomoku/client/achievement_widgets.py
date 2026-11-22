"""成就勋章的绘制与成就墙面板。"""

from __future__ import annotations

from typing import Mapping, Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.paper_panel import InkDivider, PaperPanel
from gomoku.client.theme_palette import GOLD, PAPER_LIGHT
from gomoku.shared.achievements import (
    ACHIEVEMENTS,
    ACHIEVEMENT_BY_KEY,
    ACHIEVEMENT_TOTAL,
    AchievementSpec,
    achievement_progress,
    normalize_achievement_keys,
    stats_from_mapping,
    unlocked_achievement_keys,
)

#: 未解锁勋章的灰色。
_LOCKED_BG = "#B9B1A5"
_LOCKED_RING = "#CEC6B9"
_LOCKED_INK = "#F4EFE6"


def achievement_pixmap(
    spec: AchievementSpec | str,
    size: int = 44,
    dpr: float = 1.0,
    *,
    unlocked: bool = True,
) -> QPixmap:
    """绘制圆形勋章：外环 + 内盘 + 单字。"""
    if isinstance(spec, str):
        found = ACHIEVEMENT_BY_KEY.get(spec)
        if found is None:
            return QPixmap()
        spec = found
    safe_size = max(8, int(size))
    scale = max(1.0, float(dpr))
    pixels = max(1, round(safe_size * scale))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)

    background, foreground = spec.colors
    if not unlocked:
        background = _LOCKED_BG
        foreground = _LOCKED_INK

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(scale, scale)

    rect = QRectF(1.2, 1.2, safe_size - 2.4, safe_size - 2.4)
    ring_color = QColor(GOLD if unlocked else _LOCKED_RING)
    painter.setBrush(QColor(background))
    painter.setPen(QPen(ring_color, max(1.0, safe_size * 0.06)))
    painter.drawEllipse(rect)

    inner = rect.adjusted(
        safe_size * 0.11,
        safe_size * 0.11,
        -safe_size * 0.11,
        -safe_size * 0.11,
    )
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(
        QPen(
            QColor(PAPER_LIGHT if unlocked else "#EEE8DD"),
            max(0.8, safe_size * 0.025),
        )
    )
    painter.drawEllipse(inner)

    font = QFont()
    font.setPixelSize(max(8, int(safe_size * 0.40)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(foreground))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, spec.glyph)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def _progress_text(
    spec: AchievementSpec,
    user: Mapping,
    unlocked: bool,
) -> str:
    stats = stats_from_mapping(user)
    current, target, _percent = achievement_progress(spec, stats)
    if unlocked:
        return "已解锁"
    if spec.category == "rank":
        return f"最高积分 {current} / {target}"
    if spec.category == "streak":
        return f"最高连胜 {current} / {target}"
    return f"排位胜场 {current} / {target}"


def _progress_percent(
    spec: AchievementSpec, user: Mapping
) -> int:
    stats = stats_from_mapping(user)
    _current, _target, percent = achievement_progress(spec, stats)
    return percent


class AchievementPanel(PaperPanel):
    """一名玩家的完整成就墙（未解锁的显示进度）。"""

    def __init__(
        self,
        user: Mapping,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            parent,
            seal_text="章",
            window_title="成就与勋章",
            min_width=590,
            min_height=540,
        )
        self._user = dict(user)
        username = str(self._user.get("username", "玩家"))
        stats = stats_from_mapping(self._user)
        raw_keys = self._user.get("achievements")
        if isinstance(raw_keys, (list, tuple)):
            raw_keys = normalize_achievement_keys(raw_keys)
        else:
            raw_keys = unlocked_achievement_keys(stats)
        self._user["achievements"] = list(raw_keys)
        unlocked_count = len(raw_keys)

        title = QLabel("成就与勋章", self.card)
        title.setObjectName("socialPageTitle")
        subtitle = QLabel(
            f"{username} · 已获 {unlocked_count} / {ACHIEVEMENT_TOTAL} 枚",
            self.card,
        )
        subtitle.setObjectName("socialSubtitle")
        subtitle.setWordWrap(True)
        self.content.addWidget(title)
        self.content.addWidget(subtitle)

        summary = QLabel(
            "排位胜场 "
            f"{stats.wins} · 当前连胜 {stats.current_streak} · "
            f"最高连胜 {stats.best_streak} · 最高积分 {stats.peak_rating}",
            self.card,
        )
        summary.setObjectName("achievementSummary")
        summary.setWordWrap(True)
        self.content.addWidget(summary)
        self.content.addWidget(InkDivider(self.card))

        scroll = QScrollArea(self.card)
        scroll.setObjectName("achievementScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        body = QWidget(scroll)
        body.setObjectName("achievementBody")
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        for index, spec in enumerate(ACHIEVEMENTS):
            grid.addWidget(
                self._tile(spec, body), index // 2, index % 2
            )
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        scroll.setWidget(body)
        self.content.addWidget(scroll, 1)

        close_button = QPushButton("关闭", self.card)
        close_button.setObjectName("socialSecondaryButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.close)
        self.content.addWidget(
            close_button, 0, Qt.AlignmentFlag.AlignRight
        )

    # ------------------------------------------------------------------
    def _tile(self, spec: AchievementSpec, parent: QWidget) -> QFrame:
        unlocked_keys = self._user.get("achievements", ())
        unlocked = (
            isinstance(unlocked_keys, (list, tuple))
            and spec.key in unlocked_keys
        )
        tile = QFrame(parent)
        tile.setObjectName("achievementTile")
        tile.setProperty("unlocked", "true" if unlocked else "false")
        tile.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(tile)
        row.setContentsMargins(10, 9, 10, 9)
        row.setSpacing(10)

        badge = QLabel(tile)
        badge.setPixmap(
            achievement_pixmap(
                spec,
                44,
                self.devicePixelRatioF(),
                unlocked=unlocked,
            )
        )
        badge.setFixedSize(44, 44)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel(spec.name, tile)
        name.setObjectName("achievementName")
        name.setProperty("unlocked", "true" if unlocked else "false")
        description = QLabel(spec.description, tile)
        description.setObjectName("achievementDesc")
        description.setWordWrap(True)
        progress_label = QLabel(
            _progress_text(spec, self._user, unlocked), tile
        )
        progress_label.setObjectName("achievementProgress")
        text.addWidget(name)
        text.addWidget(description)
        text.addWidget(progress_label)

        bar = QProgressBar(tile)
        bar.setObjectName("achievementProgressBar")
        bar.setRange(0, 100)
        bar.setValue(
            100 if unlocked else _progress_percent(spec, self._user)
        )
        bar.setTextVisible(False)
        bar.setFixedHeight(4)
        text.addWidget(bar)
        row.addLayout(text, 1)
        return tile


def achievement_names(keys: object) -> str:
    """把成就 key 列表转成“名字 · 名字”的中文提示。"""
    if not isinstance(keys, (list, tuple)):
        return ""
    names = [
        ACHIEVEMENT_BY_KEY[key].name
        for key in keys
        if isinstance(key, str) and key in ACHIEVEMENT_BY_KEY
    ]
    return " · ".join(names)


__all__ = [
    "achievement_pixmap",
    "achievement_names",
    "AchievementPanel",
]
