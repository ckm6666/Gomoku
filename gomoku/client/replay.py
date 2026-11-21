"""棋谱时间线：把完整落子历史转换成逐帧只读棋盘快照。"""

from __future__ import annotations

from dataclasses import dataclass

from gomoku.client.board_view import BoardView
from gomoku.shared.game_model import GameModel
from gomoku.shared.record import GameRecord


class ReplayError(ValueError):
    """棋谱无法重放。"""


@dataclass(frozen=True)
class ReplayTimeline:
    """一条棋谱的逐手快照；第 0 帧为空棋盘。"""

    record: GameRecord
    frames: tuple[BoardView, ...]

    @classmethod
    def from_record(cls, record: GameRecord) -> "ReplayTimeline":
        model = GameModel()
        frames: list[BoardView] = [BoardView.from_model(model)]

        for index, move in enumerate(record.moves, start=1):
            if model.current_player is not move.player:
                raise ReplayError(f"第 {index} 手棋手顺序不一致")
            if not model.place_stone(move.row, move.col):
                raise ReplayError(f"第 {index} 手落子非法")
            frames.append(BoardView.from_model(model))

        non_board_ending = (
            record.mode == "online"
            and record.result_reason in ("resign", "timeout")
        )
        if (
            record.winner is not None
            and model.winner is not record.winner
            and not non_board_ending
        ):
            raise ReplayError("棋谱记录的胜者与最终局面不一致")

        return cls(record=record, frames=tuple(frames))

    @property
    def last_index(self) -> int:
        return len(self.frames) - 1

    @property
    def move_count(self) -> int:
        return len(self.record.moves)

    @property
    def setup_index(self) -> int:
        """残局棋谱的初始局面帧；普通对局为 0。"""
        raw = self.record.metadata.get("setup_move_count", 0)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(value, self.last_index))

    def frame_at(self, index: int) -> BoardView:
        """返回指定手数的快照；越界时夹到首/末帧。"""
        bounded = max(0, min(int(index), self.last_index))
        return self.frames[bounded]

    def move_description(self, index: int) -> str:
        """返回“第 N 手 黑 H8”一类的落子描述；index 为帧序号。"""
        if index <= 0 or index > self.move_count:
            return "起始局面"
        move = self.record.moves[index - 1]
        column = chr(ord("A") + move.col)
        return f"第 {index} 手　{move.player.label}　{column}{move.row + 1}"


__all__ = ["ReplayError", "ReplayTimeline"]
