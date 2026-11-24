"""残局关卡数据模型与校验（纯 Python，不依赖 Qt）。

一个残局由“初始落子历史 + 先手方 + 限手数 + 可选解答线”组成。初始历史
必须从空棋盘开始黑白交替；解答线用于题库校验和后续提示，不直接展示给玩家。
本模块可被客户端题库加载器、测试和未来的服务端残局功能复用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import Move, Player, in_bounds

# 防守 AI 难度；与 client.ai_engine.AIDifficulty 的取值保持一致，
# 但 shared 层不导入 Qt/AI 模块。
DEFENDER_DIFFICULTIES = frozenset({"easy", "medium", "hard"})


class PuzzleError(ValueError):
    """残局题库数据非法。"""


class PuzzleDifficulty(Enum):
    """残局难度。"""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"

    @property
    def label(self) -> str:
        return {
            PuzzleDifficulty.EASY: "入门",
            PuzzleDifficulty.MEDIUM: "进阶",
            PuzzleDifficulty.HARD: "精解",
        }[self]


def _parse_player(value: Any, field: str) -> Player:
    if value == "black":
        return Player.BLACK
    if value == "white":
        return Player.WHITE
    raise PuzzleError(f"{field} 必须是 'black' 或 'white'")


def _parse_coord(value: Any, field: str) -> tuple[int, int]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 2
    ):
        raise PuzzleError(f"{field} 必须是 [row, col]")
    row, col = value
    if (
        not isinstance(row, int)
        or isinstance(row, bool)
        or not isinstance(col, int)
        or isinstance(col, bool)
    ):
        raise PuzzleError(f"{field} 坐标必须是整数")
    if not in_bounds(row, col):
        raise PuzzleError(f"{field} 坐标越界")
    return row, col


def _parse_moves(value: Any) -> tuple[Move, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PuzzleError("moves 必须是坐标数组")
    moves: list[Move] = []
    for index, item in enumerate(value):
        row, col = _parse_coord(item, f"moves[{index}]")
        player = Player.BLACK if index % 2 == 0 else Player.WHITE
        moves.append(Move(row=row, col=col, player=player))
    return tuple(moves)


def _parse_solution(value: Any) -> tuple[tuple[int, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PuzzleError("solution 必须是坐标数组")
    return tuple(
        _parse_coord(item, f"solution[{index}]")
        for index, item in enumerate(value)
    )


@dataclass(frozen=True)
class Puzzle:
    """一个残局关卡。"""

    id: str
    title: str
    difficulty: PuzzleDifficulty
    moves: tuple[Move, ...]
    to_move: Player
    max_player_moves: int
    description: str = ""
    hint: str = ""
    defender: str = "hard"
    solution: tuple[tuple[int, int], ...] = ()
    #: 数据源是否声明了解答线。``from_dict`` 在 JSON 明确给出 ``solution``
    #: 时置为 True，此时空解答线视为数据错误；直接构造默认 False，允许
    #: “只给初始局面、不预演解答”的关卡（题库和测试都依赖这种用法）。
    requires_solution: bool = False

    def __post_init__(self) -> None:
        # 直接构造 Puzzle(...) 也必须走完整校验，不能绕过 from_dict 的检查。
        self.validate()

    @property
    def setup_move_count(self) -> int:
        """初始局面包含的落子数。"""
        return len(self.moves)

    @property
    def has_solution(self) -> bool:
        """是否提供了可预演的解答线；空解答线返回 False。"""
        return bool(self.solution)

    @property
    def solution_moves(self) -> tuple[Move, ...]:
        """把解答坐标转换为带棋手颜色的完整落子序列。"""
        moves: list[Move] = []
        player = self.to_move
        for row, col in self.solution:
            moves.append(Move(row=row, col=col, player=player))
            player = player.opponent
        return tuple(moves)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Puzzle":
        """从 JSON 兼容字典构造并校验关卡。"""
        if not isinstance(data, Mapping):
            raise PuzzleError("残局条目必须是对象")

        try:
            raw_id = data["id"]
            raw_title = data["title"]
            raw_difficulty = data["difficulty"]
            raw_moves = data["moves"]
            raw_to_move = data["to_move"]
            raw_limit = data["max_player_moves"]
        except KeyError as exc:
            raise PuzzleError(f"残局缺少字段：{exc.args[0]}") from exc

        if not isinstance(raw_id, str) or not raw_id.strip():
            raise PuzzleError("id 必须是非空字符串")
        if not isinstance(raw_title, str) or not raw_title.strip():
            raise PuzzleError("title 必须是非空字符串")
        if not isinstance(raw_difficulty, str):
            raise PuzzleError("difficulty 必须是字符串")
        try:
            difficulty = PuzzleDifficulty(raw_difficulty)
        except ValueError as exc:
            raise PuzzleError(
                "difficulty 必须是 easy/medium/hard"
            ) from exc

        if (
            not isinstance(raw_limit, int)
            or isinstance(raw_limit, bool)
            or not 1 <= raw_limit <= 9
        ):
            raise PuzzleError("max_player_moves 必须是 1～9 的整数")

        description = data.get("description", "")
        hint = data.get("hint", "")
        if not isinstance(description, str) or not isinstance(hint, str):
            raise PuzzleError("description/hint 必须是字符串")

        defender = data.get("defender", "hard")
        if not isinstance(defender, str) or defender not in DEFENDER_DIFFICULTIES:
            raise PuzzleError("defender 必须是 easy/medium/hard")

        raw_solution = data.get("solution")
        return cls(
            id=raw_id.strip(),
            title=raw_title.strip(),
            difficulty=difficulty,
            moves=_parse_moves(raw_moves),
            to_move=_parse_player(raw_to_move, "to_move"),
            max_player_moves=raw_limit,
            description=description.strip(),
            hint=hint.strip(),
            defender=defender,
            solution=_parse_solution(raw_solution),
            # 明确写了 solution 字段（哪怕是空数组）就要求解答线非空；
            # 字段缺失或为 null 表示“本题没有预演解答”。
            requires_solution=raw_solution is not None,
        )

    def validate(self) -> None:
        """校验字段、初始局面与解答线；非法时抛出 ``PuzzleError``。

        该方法是幂等的：构造对象时由 ``__post_init__`` 调用一次，调用方
        也可以重复调用以复查动态拼装的关卡。
        """
        if not isinstance(self.id, str) or not self.id.strip():
            raise PuzzleError("id 必须是非空字符串")
        if not isinstance(self.title, str) or not self.title.strip():
            raise PuzzleError("title 必须是非空字符串")
        if not isinstance(self.difficulty, PuzzleDifficulty):
            raise PuzzleError("difficulty 必须是 PuzzleDifficulty")
        if not isinstance(self.to_move, Player):
            raise PuzzleError("to_move 必须是 Player")
        if (
            not isinstance(self.max_player_moves, int)
            or isinstance(self.max_player_moves, bool)
            or not 1 <= self.max_player_moves <= 9
        ):
            raise PuzzleError("max_player_moves 必须是 1～9 的整数")
        if (
            not isinstance(self.defender, str)
            or self.defender not in DEFENDER_DIFFICULTIES
        ):
            raise PuzzleError("defender 必须是 easy/medium/hard")
        if not isinstance(self.description, str) or not isinstance(
            self.hint, str
        ):
            raise PuzzleError("description/hint 必须是字符串")

        # 直接构造时 moves/solution 没经过 _parse_* 校验，这里补上类型与
        # 坐标范围检查；否则浮点或越界坐标会一路传到 GameModel 才报错。
        if not isinstance(self.moves, Sequence) or isinstance(
            self.moves, (str, bytes)
        ):
            raise PuzzleError("moves 必须是 Move 数组")
        for index, move in enumerate(self.moves):
            if not isinstance(move, Move):
                raise PuzzleError(f"moves[{index}] 必须是 Move 对象")
            _parse_coord((move.row, move.col), f"moves[{index}]")
        if not isinstance(self.solution, Sequence) or isinstance(
            self.solution, (str, bytes)
        ):
            raise PuzzleError("solution 必须是坐标数组")
        for index, coord in enumerate(self.solution):
            _parse_coord(coord, f"solution[{index}]")

        try:
            model = GameModel.from_moves(self.moves)
        except ValueError as exc:
            raise PuzzleError(f"初始局面非法：{exc}") from exc

        if model.is_game_over:
            raise PuzzleError("初始局面已经结束")
        if model.current_player is not self.to_move:
            raise PuzzleError("to_move 与初始历史的轮次不一致")

        if not self.solution:
            # 空解答线有两种来源：题库只给初始局面（合法），或数据源声明
            # 了 solution 却给空数组（数据错误）。用 requires_solution 显式
            # 区分，避免把“没有解答线”静默当成校验通过。
            if self.requires_solution:
                raise PuzzleError("该关卡声明了解答线，但 solution 为空")
            return

        expected_length = self.max_player_moves * 2 - 1
        if len(self.solution) != expected_length:
            raise PuzzleError(
                f"解答线长度应为 {expected_length}，实际为 {len(self.solution)}"
            )

        probe = GameModel.from_moves(self.moves)
        last_index = len(self.solution) - 1
        for index, (row, col) in enumerate(self.solution):
            if not probe.place_stone(row, col):
                raise PuzzleError(f"解答线第 {index + 1} 手非法")
            if probe.is_game_over and index != last_index:
                raise PuzzleError(f"解答线在第 {index + 1} 手提前结束")
        if probe.winner is not self.to_move:
            raise PuzzleError("解答线最后一步必须由先手方形成五连")


__all__ = [
    "DEFENDER_DIFFICULTIES",
    "Puzzle",
    "PuzzleDifficulty",
    "PuzzleError",
]
