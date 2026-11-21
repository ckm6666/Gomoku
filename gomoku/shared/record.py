"""可持久化/传输的对局棋谱模型（纯 Python，不依赖 Qt）。

本地棋谱以 JSON Lines 保存，联机棋谱接口也复用同一套落子结构。载入时用
:class:`~gomoku.shared.game_model.GameModel` 重放全部落子，确保损坏/篡改的
棋谱不会进入复盘界面。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import Move, Player

RECORD_VERSION = 1
RECORD_MODES = frozenset({"local", "ai", "puzzle", "online"})


class RecordError(ValueError):
    """棋谱数据非法或已损坏。"""


def _parse_player(value: Any, field_name: str) -> Player:
    if value == "black":
        return Player.BLACK
    if value == "white":
        return Player.WHITE
    raise RecordError(f"{field_name} 必须是 'black' 或 'white'")


def _parse_optional_player(value: Any) -> Player | None:
    if value is None:
        return None
    return _parse_player(value, "winner")


def _parse_moves(value: Any) -> tuple[Move, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecordError("moves 必须是数组")
    moves: list[Move] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise RecordError(f"moves[{index}] 必须是对象")
        try:
            row = item["row"]
            col = item["col"]
            player = _parse_player(item["player"], f"moves[{index}].player")
        except KeyError as exc:
            raise RecordError(f"moves[{index}] 缺少字段：{exc.args[0]}") from exc
        if (
            not isinstance(row, int)
            or isinstance(row, bool)
            or not isinstance(col, int)
            or isinstance(col, bool)
        ):
            raise RecordError(f"moves[{index}] 坐标必须是整数")
        moves.append(Move(row=row, col=col, player=player))
    return tuple(moves)


@dataclass(frozen=True)
class GameRecord:
    """一局完整棋谱。``moves`` 从空棋盘开始，包含全部落子。"""

    record_id: str
    created_at: str
    mode: str
    moves: tuple[Move, ...]
    winner: Player | None
    result_reason: str = ""
    black_name: str = ""
    white_name: str = ""
    rated: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GameRecord":
        """从 JSON 字典构造并校验棋谱。"""
        if not isinstance(data, Mapping):
            raise RecordError("棋谱必须是对象")

        version = data.get("version", RECORD_VERSION)
        if version != RECORD_VERSION:
            raise RecordError(f"不支持的棋谱版本：{version}")

        record_id = data.get("record_id")
        created_at = data.get("created_at")
        mode = data.get("mode")
        if not isinstance(record_id, str) or not record_id.strip():
            raise RecordError("record_id 必须是非空字符串")
        if not isinstance(created_at, str) or not created_at.strip():
            raise RecordError("created_at 必须是非空字符串")
        if not isinstance(mode, str) or mode not in RECORD_MODES:
            raise RecordError("mode 必须是 local/ai/puzzle/online")

        moves = _parse_moves(data.get("moves"))
        winner = _parse_optional_player(data.get("winner"))

        result_reason = data.get("result_reason", "")
        black_name = data.get("black_name", "")
        white_name = data.get("white_name", "")
        for field_name, value in (
            ("result_reason", result_reason),
            ("black_name", black_name),
            ("white_name", white_name),
        ):
            if not isinstance(value, str):
                raise RecordError(f"{field_name} 必须是字符串")

        rated = data.get("rated", False)
        if not isinstance(rated, bool):
            raise RecordError("rated 必须是布尔值")

        metadata = data.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise RecordError("metadata 必须是对象")

        try:
            model = GameModel.from_moves(moves)
        except ValueError as exc:
            raise RecordError(f"棋谱落子非法：{exc}") from exc

        # 联机对局可能因认输/超时结束：胜者成立，但棋盘上未必有五连。
        non_board_ending = mode == "online" and result_reason.strip() in (
            "resign",
            "timeout",
        )
        if (
            winner is not None
            and model.winner is not winner
            and not non_board_ending
        ):
            raise RecordError("winner 与棋谱最终局面不一致")
        if winner is None and model.winner is not None:
            raise RecordError("棋谱已有胜者但 winner 为空")

        return cls(
            record_id=record_id.strip(),
            created_at=created_at.strip(),
            mode=mode,
            moves=moves,
            winner=winner,
            result_reason=result_reason.strip(),
            black_name=black_name.strip(),
            white_name=white_name.strip(),
            rated=rated,
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 兼容字典。"""
        return {
            "version": RECORD_VERSION,
            "record_id": self.record_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "winner": None if self.winner is None else self.winner.value,
            "result_reason": self.result_reason,
            "black_name": self.black_name,
            "white_name": self.white_name,
            "rated": self.rated,
            "moves": [
                {
                    "row": move.row,
                    "col": move.col,
                    "player": move.player.value,
                }
                for move in self.moves
            ],
            "metadata": dict(self.metadata),
        }


__all__ = [
    "RECORD_MODES",
    "RECORD_VERSION",
    "GameRecord",
    "RecordError",
]
