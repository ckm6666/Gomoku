"""残局挑战关卡选择页。"""

from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.daily_puzzle import (
    daily_puzzle,
    load_progress,
    record_result,
)
from gomoku.client.puzzle_card import PuzzleCardButton
from gomoku.client.ui_theme import MENU_BACKDROP_SPEC, InkBackdrop
from gomoku.shared.puzzle import Puzzle
from gomoku.shared.rules import Player


class PuzzlePage(InkBackdrop):
    """平铺展示残局卡片，每张卡以成语作为进入按钮标题。"""

    puzzle_selected = Signal(object)
    daily_selected = Signal(object)
    back_requested = Signal()

    def __init__(
        self,
        puzzles: Sequence[Puzzle],
        parent: Optional[QWidget] = None,
        *,
        settings: Optional[QSettings] = None,
    ) -> None:
        super().__init__(parent, spec=MENU_BACKDROP_SPEC)
        self.setObjectName("puzzlePage")
        self._puzzles = tuple(puzzles)
        self._settings = settings or QSettings()
        self._daily_puzzle: Optional[Puzzle] = None
        self._daily_completed = False
        self._daily_streak = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(10)

        header = QHBoxLayout()
        title_layout = QVBoxLayout()
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)

        title = QLabel("残局挑战", self)
        title.setObjectName("brandTitle")
        subtitle = QLabel(
            "精选残局，白先或黑先，限手数内连成五子；对手由本地 AI 防守。",
            self,
        )
        subtitle.setObjectName("brandSubtitle")
        subtitle.setWordWrap(True)
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)

        self.count_label = QLabel(self)
        self.count_label.setObjectName("puzzleProgress")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        header.addLayout(title_layout)
        header.addStretch(1)
        header.addWidget(
            self.count_label, 0, Qt.AlignmentFlag.AlignVCenter
        )
        root.addLayout(header)

        scroll = QScrollArea(self)
        scroll.setObjectName("puzzleScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        viewport = scroll.viewport()
        viewport.setAutoFillBackground(False)
        viewport.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )

        container = QWidget(scroll)
        container.setObjectName("puzzleScrollBody")
        container.setAutoFillBackground(False)
        self._body_layout = QVBoxLayout(container)
        self._body_layout.setContentsMargins(8, 6, 8, 6)
        self._body_layout.setSpacing(14)
        self._body_layout.addStretch(1)

        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(2, 0, 2, 0)
        footer.addStretch(1)
        self.back_button = QPushButton("返回主菜单", self)
        self.back_button.setObjectName("menuBackButton")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setMinimumWidth(110)
        self.back_button.setMinimumHeight(38)
        self.back_button.setAutoDefault(False)
        self.back_button.clicked.connect(
            lambda _checked=False: self.back_requested.emit()
        )
        footer.addWidget(self.back_button)
        root.addLayout(footer)

        self.refresh_daily()

    # ------------------------------------------------------------------
    # 每日残局
    # ------------------------------------------------------------------
    def refresh_daily(self) -> None:
        """重新计算今日残局并刷新卡片。"""
        today = date.today()
        self._daily_puzzle = daily_puzzle(self._puzzles, today)
        last_date, streak = load_progress(self._settings)
        self._daily_completed = bool(
            self._daily_puzzle is not None
            and last_date == today.isoformat()
        )
        self._daily_streak = streak
        self._rebuild()

    def record_daily_result(self, puzzle_id: str, success: bool) -> None:
        """如果完成的是今日残局，则更新连续通关进度。"""
        today = date.today()
        current = daily_puzzle(self._puzzles, today)
        if current is None or current.id != puzzle_id:
            return
        record_result(self._settings, today, success)
        self.refresh_daily()

    def _build_daily_card(self, parent: QWidget) -> QFrame:
        puzzle = self._daily_puzzle
        assert puzzle is not None
        card = QFrame(parent)
        card.setObjectName("dailyPuzzleCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(18, 14, 18, 14)
        row.setSpacing(12)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        title = QLabel("每日残局", card)
        title.setObjectName("sectionTitle")
        self.daily_title_label = QLabel(
            f"{puzzle.title} · {side_text(puzzle)} · "
            f"限 {puzzle.max_player_moves} 手",
            card,
        )
        self.daily_title_label.setObjectName("dailyPuzzleTitle")
        if self._daily_completed:
            status_text = f"今日已通关 · 连续 {self._daily_streak} 天"
        elif self._daily_streak:
            status_text = f"连续 {self._daily_streak} 天 · 今日待挑战"
        else:
            status_text = "今日尚未挑战"
        self.daily_status_label = QLabel(status_text, card)
        self.daily_status_label.setObjectName("dailyPuzzleStatus")
        text_layout.addWidget(title)
        text_layout.addWidget(self.daily_title_label)
        text_layout.addWidget(self.daily_status_label)

        self.daily_button = QPushButton(
            "再次挑战" if self._daily_completed else "挑战今日残局",
            card,
        )
        self.daily_button.setObjectName("smallButton")
        self.daily_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.daily_button.setAutoDefault(False)
        self.daily_button.clicked.connect(
            lambda _checked=False: self.daily_selected.emit(puzzle)
        )

        row.addLayout(text_layout, 1)
        row.addWidget(self.daily_button, 0)
        return card

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _rebuild(self) -> None:
        # 清空除末尾 stretch 外的旧内容。
        while self._body_layout.count() > 1:
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        total = len(self._puzzles)
        self.count_label.setText(
            f"共 {total} 关" if total else "暂无关卡"
        )

        if not self._puzzles:
            empty = QLabel(
                "暂无可用的残局关卡，请检查残局题库文件。", self
            )
            empty.setObjectName("hintLabel")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body_layout.insertWidget(0, empty)
            return

        insert_at = 0
        if self._daily_puzzle is not None:
            self._body_layout.insertWidget(
                0, self._build_daily_card(self)
            )
            insert_at = 1

        group = QWidget(self)
        group.setObjectName("puzzleGroup")
        grid = QGridLayout(group)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for index, puzzle in enumerate(self._puzzles):
            grid.addWidget(
                self._build_card(group, puzzle), index // 2, index % 2
            )
        self._body_layout.insertWidget(insert_at, group)

    def _build_card(self, parent: QWidget, puzzle: Puzzle) -> PuzzleCardButton:
        button = PuzzleCardButton(
            puzzle.title,
            side_text(puzzle),
            puzzle.max_player_moves,
            hint=puzzle.hint or puzzle.description,
            parent=parent,
        )
        button.clicked.connect(
            lambda _checked=False, puzzle=puzzle: self.puzzle_selected.emit(
                puzzle
            )
        )
        return button


def side_text(puzzle: Puzzle) -> str:
    return "白先" if puzzle.to_move is Player.WHITE else "黑先"


__all__ = ["PuzzlePage", "side_text"]
