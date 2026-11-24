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
from gomoku.shared.rules import (
    NON_BOARD_ENDINGS,
    RESULT_REASON_DRAW,
    RESULT_REASON_FIVE,
    RESULT_REASONS,
    Move,
    Player,
    parse_player,
    parse_rule_mode,
)

RECORD_VERSION = 1
RECORD_MODES = frozenset({"local", "ai", "puzzle", "online"})


class RecordError(ValueError):
    """棋谱数据非法或已损坏。"""


def _parse_player(value: Any, field_name: str) -> Player:
    return parse_player(value, RecordError, field_name)


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
    """一局棋谱。``moves`` 从空棋盘开始，包含全部落子。

    已结束的对局带 ``winner``/``result_reason``；``winner=None`` 且
    ``result_reason`` 为空表示“还没下完”的快照（例如导入未完成的 SGF），
    这类记录不得声称任何结果。

    ``rule_mode`` 记录对局采用的规则：``"freestyle"``（默认，自由规则）
    或 ``"renju"``（连珠规则）。载入连珠棋谱时按连珠规则重放，黑棋禁手
    会被当作数据损坏拒绝；旧棋谱没有该字段时按自由规则处理。
    """

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
    rule_mode: str = "freestyle"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GameRecord":
        """从 JSON 字典构造并校验棋谱。"""
        if not isinstance(data, Mapping):
            raise RecordError("棋谱必须是对象")

        version = data.get("version", RECORD_VERSION)
        # 用精确类型判断：bool 是 int 的子类，``True == 1`` 会让版本号
        # 1 的棋谱被当成合法数据放行。
        if type(version) is not int or version != RECORD_VERSION:
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
        # 终局原因必须来自已知集合：未知原因会让棋谱与统计口径脱节
        # （例如 winner=BLACK + "draw" 这种自相矛盾的组合）。
        reason = result_reason.strip()
        if reason and reason not in RESULT_REASONS:
            raise RecordError(
                "result_reason 必须是 "
                + "/".join(sorted(RESULT_REASONS))
            )

        rated = data.get("rated", False)
        if not isinstance(rated, bool):
            raise RecordError("rated 必须是布尔值")

        # 规则模式决定重放时是否启用连珠禁手；缺省按自由规则处理，
        # 与旧棋谱完全兼容。未知取值属于数据损坏，必须拒绝。
        try:
            rule_mode = parse_rule_mode(
                data.get("rule_mode", "freestyle")
            ).value
        except ValueError as exc:
            raise RecordError(
                "rule_mode 必须是 'freestyle' 或 'renju'"
            ) from exc

        metadata = data.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise RecordError("metadata 必须是对象")

        try:
            model = GameModel.from_moves(moves, mode=rule_mode)
        except ValueError as exc:
            raise RecordError(f"棋谱落子非法：{exc}") from exc

        # 棋盘与结果必须自洽。
        #
        # * 棋盘已经分出胜负时，记录里的胜者必须与棋盘一致——即使写着
        #   “认输/超时”也不行，否则会导出互相矛盾的棋谱。
        # * 棋盘没有五连时，只有“非棋盘结束”（认输/超时/逃跑）的联机
        #   棋谱才允许有胜者。
        # * 棋谱宣称已经结束（result_reason 非空）却没有分出胜负时，
        #   说明记录与棋盘互相矛盾，必须拒绝。
        #
        # 注意：result_reason 为空且没有胜者的棋谱是“这局还没下完”的
        # 快照（复盘、AI 思考中都会保存），属于合法数据，不能拒绝。
        if model.winner is not None:
            if winner is None:
                raise RecordError("棋谱已有胜者但 winner 为空")
            if winner is not model.winner:
                raise RecordError("winner 与棋谱最终局面不一致")
            # 棋盘上已经成五，说明对局是在棋盘上结束的；和棋/认输/超时/
            # 逃跑都不可能同时成立（例如 winner=BLACK + "draw"）。留空
            # 是为了兼容没有 result_reason 的旧棋谱。
            if reason and reason != RESULT_REASON_FIVE:
                raise RecordError(
                    "棋盘已分出胜负，result_reason 必须是 five 或留空"
                )
        else:
            non_board_ending = (
                mode == "online" and reason in NON_BOARD_ENDINGS
            )
            if winner is not None and not non_board_ending:
                raise RecordError("棋谱没有成五却记录了胜者")
            if reason and not non_board_ending:
                if reason == RESULT_REASON_DRAW:
                    # 和棋只能对应“棋盘下满且没人获胜”；满盘平局本身
                    # 是合法记录，不能因为棋盘没有胜者就拒绝。
                    if not model.is_draw:
                        raise RecordError(
                            "棋谱记录为和棋但最终局面不是满盘"
                        )
                elif reason == RESULT_REASON_FIVE:
                    raise RecordError(
                        "棋谱记录为五连终局但棋盘上没有五连"
                    )
                else:
                    raise RecordError("棋谱结果与最终局面不一致")

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
            rule_mode=rule_mode,
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
            "rule_mode": self.rule_mode,
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
    "NON_BOARD_ENDINGS",
    "RECORD_MODES",
    "RECORD_VERSION",
    "RESULT_REASONS",
    "GameRecord",
    "RecordError",
]
