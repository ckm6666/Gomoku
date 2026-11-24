"""五子棋对局状态模型（纯 Python，不依赖 Qt）。

胜线规则位于 :mod:`gomoku.shared.rules`，客户端界面、AI 与未来服务端都复用
同一份规则。本模块只维护一局棋的棋盘、当前棋手和本地悔棋/重置语义；联机
房间、座位、时钟和服务器裁决由未来的 OnlineSession/服务端提供。
"""

from __future__ import annotations

from typing import Sequence

from gomoku.shared.rules import (
    BOARD_CELL_COUNT,
    BOARD_SIZE,
    DEFAULT_RULE_MODE,
    WIN_LENGTH,
    BoardLike,
    Cell,
    Move,
    Player,
    RuleMode,
    find_winning_line,
    in_bounds,
    is_forbidden_move,
    parse_rule_mode,
)


class GameModel:
    """15×15 五子棋的对局状态与规则。

    默认采用休闲五子棋自由规则：黑棋先行，任意一方在横、竖或两条斜线
    上形成连续 5 颗及以上同色棋子即获胜；不设禁手。传入
    :class:`~gomoku.shared.rules.RuleMode.RENJU` 时启用简化的连珠规则：
    黑棋的长连、四四、三三禁手会被拒绝，黑棋只有恰好五连才算获胜；
    白棋不受禁手限制，长连仍算获胜。
    """

    def __init__(self, mode: RuleMode | str = DEFAULT_RULE_MODE) -> None:
        """创建空棋盘；``mode`` 接受 :class:`RuleMode` 或 ``"freestyle"`` /
        ``"renju"`` 字符串。"""
        self._rule_mode = parse_rule_mode(mode)
        # 棋盘使用二维列表保存，行列索引与界面坐标一致。
        self._board: list[list[Cell]] = [
            [None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
        ]
        self._current_player: Player = Player.BLACK
        self._moves: list[Move] = []
        self._winner: Player | None = None
        self._winning_line: tuple[tuple[int, int], ...] = ()
        self._is_draw = False

    @classmethod
    def from_moves(
        cls,
        moves: Sequence[Move],
        mode: RuleMode | str = DEFAULT_RULE_MODE,
    ) -> "GameModel":
        """从完整落子历史重建模型。

        用于载入残局初始局面和复盘棋谱。历史必须从空棋盘开始、黑白交替、
        每手合法；一旦对局已经结束仍有后续落子，则拒绝载入。非法历史抛出
        ``ValueError``，调用方应把它当作数据损坏处理。

        ``mode`` 决定使用自由规则还是连珠规则；连珠模式下若历史上出现
        禁手，同样以 ``ValueError`` 拒绝。
        """
        model = cls(mode)
        for index, move in enumerate(moves, start=1):
            if not isinstance(move, Move):
                raise ValueError(f"第 {index} 手不是 Move 对象")
            # 坐标必须是精确的整数：bool 是 int 的子类，放行会让
            # ``self._board[True]`` 落到第 1 行；浮点坐标则会在索引
            # 棋盘时抛 TypeError，绕过调用方的 ``except ValueError``。
            if type(move.row) is not int or type(move.col) is not int:
                raise ValueError(f"第 {index} 手坐标必须是整数")
            if move.player is not model._current_player:
                raise ValueError(f"第 {index} 手棋手顺序不正确")
            if not model.place_stone(move.row, move.col):
                raise ValueError(f"第 {index} 手落子非法")
        return model

    # ------------------------------------------------------------------
    # 只读状态：界面/会话层读取这些属性，不直接改动内部数据
    # ------------------------------------------------------------------
    @property
    def board(self) -> tuple[tuple[Cell, ...], ...]:
        """返回只读形式的棋盘快照。"""
        return tuple(tuple(row) for row in self._board)

    @property
    def rule_mode(self) -> RuleMode:
        """当前对局使用的规则模式；默认为自由规则。"""
        return self._rule_mode

    @property
    def current_player(self) -> Player:
        """棋盘上记录的棋手。

        终局后该值仍然是最后落子的棋手（也就是胜者），并不表示“接下来
        该谁落子”。需要下一个行动方请用 :attr:`next_player`。
        """
        return self._current_player

    @property
    def next_player(self) -> Player | None:
        """下一个应当落子的棋手；对局已经结束时为 ``None``。"""
        if self.is_game_over:
            return None
        return self._current_player

    @property
    def moves(self) -> tuple[Move, ...]:
        """按时间顺序排列的全部落子记录。"""
        return tuple(self._moves)

    @property
    def move_count(self) -> int:
        """当前已经落下的棋子数量。"""
        return len(self._moves)

    @property
    def last_move(self) -> Move | None:
        """最后一手；空棋盘时为 None。"""
        return self._moves[-1] if self._moves else None

    @property
    def winner(self) -> Player | None:
        """胜者；未产生胜者时为 None。"""
        return self._winner

    @property
    def winning_line(self) -> tuple[tuple[int, int], ...]:
        """构成胜利的完整连续同色棋子坐标集合。"""
        return self._winning_line

    @property
    def is_draw(self) -> bool:
        """棋盘已满且无人获胜时为平局。"""
        return self._is_draw

    @property
    def is_game_over(self) -> bool:
        """对局是否已经结束。"""
        return self._winner is not None or self._is_draw

    # ------------------------------------------------------------------
    # 规则操作
    # ------------------------------------------------------------------
    def is_legal_move(self, row: int, col: int) -> bool:
        """判断在当前局面下，本手是否可以落在指定交叉点。

        判定必须与 :meth:`place_stone` 完全一致：连珠模式下黑棋的禁手点
        同样返回 ``False``，否则调用方会认为该点可下、落子时才被拒绝。
        """
        if self.is_game_over:
            return False
        if not in_bounds(row, col):
            return False
        if self._board[row][col] is not None:
            return False
        if (
            self._rule_mode is RuleMode.RENJU
            and self._current_player is Player.BLACK
            and is_forbidden_move(
                self._board, row, col, self._current_player, self._rule_mode
            )
        ):
            return False
        return True

    def place_stone(self, row: int, col: int) -> bool:
        """在指定交叉点落下当前棋手的棋子。

        连珠模式下，黑棋若在落子前已被判定为长连、四四或三三禁手，
        本手会被拒绝：棋盘、落子记录与当前棋手都保持不变（与其他非法
        落子一致）。白棋不检查禁手。

        返回：
            成功返回 True；坐标非法（含 bool）、越界、占用、已经结束
            或在连珠模式下构成黑棋禁手时返回 False。
        """
        if self.is_game_over:
            return False
        # 先判类型再判范围：bool 是 int 的子类，不挡住就会把
        # ``place_stone(True, False)`` 当成落在 (1, 0)。
        if not in_bounds(row, col):
            return False
        if self._board[row][col] is not None:
            return False

        player = self._current_player
        if (
            self._rule_mode is RuleMode.RENJU
            and player is Player.BLACK
            and is_forbidden_move(self._board, row, col, player, self._rule_mode)
        ):
            return False

        self._board[row][col] = player
        self._moves.append(Move(row=row, col=col, player=player))

        winning_line = self._find_winning_line(row, col, player)
        if winning_line and self._is_winning_line(player, winning_line):
            self._winner = player
            self._winning_line = tuple(winning_line)
        elif len(self._moves) == BOARD_CELL_COUNT:
            self._is_draw = True
        else:
            self._current_player = player.opponent
        return True

    def _is_winning_line(
        self, player: Player, line: Sequence[tuple[int, int]]
    ) -> bool:
        """按规则模式判断连续同色段是否构成胜利。

        自由规则下调用方只在 ``line`` 非空时才会询问，任意长度均获胜。
        连珠规则下白棋连续 5 颗及以上获胜；黑棋只认恰好 5 颗——长连属于
        禁手，已在落子前被 :func:`is_forbidden_move` 拒绝，正常对局不会
        走到这里，这里只是防御性兜底。
        """
        if self._rule_mode is RuleMode.RENJU and player is Player.BLACK:
            return len(line) == WIN_LENGTH
        return True

    def undo(self) -> bool:
        """撤销最后一手；清除终局状态并把行动权交还被撤棋手。

        这是本地模式使用的单方面悔棋；联机模式必须由服务器和对手授权，
        不能直接复用该语义。
        """
        if not self._moves:
            return False

        last_move = self._moves.pop()
        self._board[last_move.row][last_move.col] = None
        self._winner = None
        self._winning_line = ()
        self._is_draw = False
        self._current_player = last_move.player
        return True

    def reset(self) -> None:
        """清空棋盘并重新由黑棋先行。"""
        self._board = [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
        self._current_player = Player.BLACK
        self._moves.clear()
        self._winner = None
        self._winning_line = ()
        self._is_draw = False

    def empty_cells(self) -> list[tuple[int, int]]:
        """返回全部空交叉点。"""
        return [
            (row, col)
            for row in range(BOARD_SIZE)
            for col in range(BOARD_SIZE)
            if self._board[row][col] is None
        ]

    # ------------------------------------------------------------------
    # 内部兼容入口
    # ------------------------------------------------------------------
    def _find_winning_line(
        self, row: int, col: int, player: Player
    ) -> list[tuple[int, int]]:
        """兼容内部调用：委托给共享无状态规则函数。"""
        return find_winning_line(self._board, row, col, player)


__all__ = [
    "DEFAULT_RULE_MODE",
    "BoardLike",
    "Cell",
    "Move",
    "Player",
    "RuleMode",
    "GameModel",
    "find_winning_line",
    "in_bounds",
]
