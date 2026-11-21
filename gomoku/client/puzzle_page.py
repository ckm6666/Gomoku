"""残局挑战关卡选择页。"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Qt, Signal
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

from gomoku.client.puzzle_card import PuzzleCardButton
from gomoku.client.ui_theme import MENU_BACKDROP_SPEC, InkBackdrop
from gomoku.shared.puzzle import Puzzle
from gomoku.shared.rules import Player


class PuzzlePage(InkBackdrop):
    """平铺展示残局卡片，每张卡以成语作为进入按钮标题。"""

    puzzle_selected = Signal(object)
    back_requested = Signal()

    def __init__(
        self,
        puzzles: Sequence[Puzzle],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, spec=MENU_BACKDROP_SPEC)
        self.setObjectName("puzzlePage")
        self._puzzles = tuple(puzzles)

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

        self._rebuild()

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
        self._body_layout.insertWidget(0, group)

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
