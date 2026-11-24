"""棋盘绘制回归：胜利连线必须完整可见，不能被最后落子标记盖住。

问题背景：原来的绘制顺序是“胜利连线 → 悬停预览 → 最后落子标记”，
朱红色的最后落子标记会压在连线中心，连线看起来缺一截。
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
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.board_view import BoardView  # noqa: E402
from gomoku.client.board_widget import BoardWidget  # noqa: E402
from gomoku.client.constants import (  # noqa: E402
    LAST_MOVE_COLOR,
    WIN_HIGHLIGHT_COLOR,
)
from gomoku.shared.rules import BOARD_SIZE, Move, Player  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])

_SIZE = 420
_WINNING_LINE = ((7, 3), (7, 4), (7, 5), (7, 6), (7, 7))
#: 最后一手落在连线正中：标记正好压在连线中心上。
_LAST_MOVE = Move(7, 5, Player.BLACK)


def _make_view(*, with_winning_line: bool) -> BoardView:
    """构造一个黑棋五连、最后一手落在连线中段的局面。"""
    board = [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
    for row, col in _WINNING_LINE:
        board[row][col] = Player.BLACK
    return BoardView(
        board=tuple(tuple(row) for row in board),
        current_player=Player.WHITE,
        last_move=_LAST_MOVE,
        winner=Player.BLACK,
        winning_line=_WINNING_LINE if with_winning_line else (),
        is_draw=False,
        is_game_over=True,
        move_count=len(_WINNING_LINE),
    )


def _render(view: BoardView) -> tuple[BoardWidget, QImage]:
    """把棋盘渲染到内存图像，返回控件与图像。"""
    widget = BoardWidget(view)
    widget.resize(_SIZE, _SIZE)
    image = QImage(_SIZE, _SIZE, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    widget.render(image)
    return widget, image


def _channel_distance(left: QColor, right: QColor) -> int:
    """两个颜色在 RGB 上的最大通道差。"""
    return max(
        abs(left.red() - right.red()),
        abs(left.green() - right.green()),
        abs(left.blue() - right.blue()),
    )


class _OrderSpyBoard(BoardWidget):
    """记录覆盖层绘制顺序的棋盘控件。"""

    def __init__(self, view: BoardView) -> None:
        super().__init__(view)
        self.calls: list[str] = []

    def _draw_winning_line(self, painter: QPainter) -> None:
        self.calls.append("winning_line")
        super()._draw_winning_line(painter)

    def _draw_last_move_marker(self, painter: QPainter) -> None:
        self.calls.append("last_move_marker")
        super()._draw_last_move_marker(painter)


class BoardPaintOrderTests(unittest.TestCase):
    """胜利连线与最后落子标记的绘制顺序。"""

    def test_winning_line_is_painted_after_last_move_marker(self) -> None:
        """连线必须后画：否则朱红标记会盖住连线中心。"""
        widget = _OrderSpyBoard(_make_view(with_winning_line=True))
        try:
            widget.resize(_SIZE, _SIZE)
            widget.render(
                QImage(_SIZE, _SIZE, QImage.Format.Format_ARGB32)
            )
            self.assertIn("last_move_marker", widget.calls)
            self.assertIn("winning_line", widget.calls)
            self.assertLess(
                widget.calls.index("last_move_marker"),
                widget.calls.index("winning_line"),
            )
        finally:
            widget.deleteLater()

    def test_winning_line_stays_visible_on_last_move_cell(self) -> None:
        """落点中心的像素应当是连线颜色，而不是标记的红色。"""
        widget, with_line = _render(_make_view(with_winning_line=True))
        other_widget, without_line = _render(
            _make_view(with_winning_line=False)
        )
        try:
            point = widget._point_for(
                _LAST_MOVE.row, _LAST_MOVE.col
            ).toPoint()
            painted = with_line.pixelColor(point)
            marker_only = without_line.pixelColor(point)
            expected = QColor(WIN_HIGHLIGHT_COLOR)
            self.assertLess(
                _channel_distance(painted, expected),
                _channel_distance(marker_only, expected),
            )
            # 没有连线时那一格才是最后落子标记的红色。
            self.assertLess(
                _channel_distance(marker_only, QColor(LAST_MOVE_COLOR)),
                _channel_distance(marker_only, expected),
            )
        finally:
            widget.deleteLater()
            other_widget.deleteLater()


if __name__ == "__main__":
    unittest.main(verbosity=2)
