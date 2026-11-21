"""棋盘只读视图。

BoardWidget 不再直接读取具体 GameModel，而是消费这个不可变快照。这样本地、
AI 以及未来的 OnlineSession 都可以把自己的状态投影成 BoardView，界面层
无需知道状态来自本地模型还是服务器快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from gomoku.shared.rules import Cell, Move, Player

if TYPE_CHECKING:
    from gomoku.shared.game_model import GameModel


@dataclass(frozen=True)
class BoardView:
    """棋盘绘制/悬停所需的只读状态快照。"""

    board: tuple[tuple[Cell, ...], ...]
    current_player: Player
    last_move: Optional[Move]
    winner: Optional[Player]
    winning_line: tuple[tuple[int, int], ...]
    is_draw: bool
    is_game_over: bool
    move_count: int

    @classmethod
    def from_model(cls, model: "GameModel") -> "BoardView":
        """从本地 GameModel 生成只读快照。

        ``GameModel.board`` 本身已经返回不可变副本；这里只做结构投影，
        不暴露模型引用，避免棋盘控件顺手修改业务状态。
        """
        return cls(
            board=model.board,
            current_player=model.current_player,
            last_move=model.last_move,
            winner=model.winner,
            winning_line=model.winning_line,
            is_draw=model.is_draw,
            is_game_over=model.is_game_over,
            move_count=model.move_count,
        )


__all__ = ["BoardView"]
