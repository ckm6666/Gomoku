"""沙盘推演模块：对联机对局局面的本地分叉演练。

沙盘（sandbox）是真实联机局面的一个"本地副本"：它以服务器快照中的棋盘
作为只读基线，允许棋手在不影响真实对局的前提下自由推演后续变化。由于
沙盘中由用户同时扮演黑、白双方，因此落子会在两方之间自动轮转，用户也可
以随时切换执子方继续推演。

重要约定：沙盘完全在客户端内存中运行，它不会向服务器发送任何落子请求，
也绝不修改真实对局的权威状态；所有推演结果仅用于本地分析。本模块不依赖
PySide6/Qt，可在无界面的单元测试中直接导入使用。
"""

from __future__ import annotations

from typing import Optional

from gomoku.client.board_view import BoardView
from gomoku.shared.rules import (
    BOARD_CELL_COUNT,
    BOARD_SIZE,
    BoardLike,
    Cell,
    Move,
    Player,
    find_winning_line,
)


def format_coordinate(row: int, col: int) -> str:
    """把行列坐标格式化成界面用的棋盘记号（例如 (7, 7) -> "H8"）。"""
    return f"{chr(ord('A') + col)}{row + 1}"


class SandboxModel:
    """联机局面的沙盘模型：只读基线 + 本地推演落子。

    基线（真实局面）在构造时被完整复制，外部持有的棋盘对象不会被修改；
    沙盘的所有推演都发生在内部副本上，可随时通过 :meth:`reset` 回到基线。
    """

    def __init__(
        self,
        board: BoardLike,
        current_player: Player,
        base_move_count: int = 0,
        last_move: Optional[Move] = None,
    ) -> None:
        """以真实局面为基线建立沙盘。

        参数：
            board: 真实局面的棋盘（15×15），构造后即被复制，不再关联。
            current_player: 基线局面中应当落子的棋手。
            base_move_count: 真实对局已经走过的手数。
            last_move: 真实对局的最后一手，用于显示最后一手标记。
        """
        if not isinstance(current_player, Player):
            raise ValueError("current_player 必须是 Player 枚举值")

        self._base_board = self._copy_board(board)
        self._board: list[list[Cell]] = [list(row) for row in self._base_board]
        self._base_current_player = current_player
        self._current_player = current_player
        self._base_move_count = base_move_count
        self._base_last_move = last_move
        self._moves: list[Move] = []
        self._last_move = last_move
        self._winner: Optional[Player] = None
        self._winning_line: tuple[tuple[int, int], ...] = ()
        self._is_draw = False

    @classmethod
    def from_view(cls, view: BoardView) -> "SandboxModel":
        """由棋盘只读快照创建沙盘，棋盘内容会被复制。"""
        return cls(
            board=view.board,
            current_player=view.current_player,
            base_move_count=view.move_count,
            last_move=view.last_move,
        )

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def board(self) -> tuple[tuple[Cell, ...], ...]:
        """返回沙盘棋盘的不可变快照。"""
        return tuple(tuple(row) for row in self._board)

    @property
    def base_move_count(self) -> int:
        """真实对局已经走过的手数。"""
        return self._base_move_count

    @property
    def moves(self) -> tuple[Move, ...]:
        """沙盘中推演落下的棋谱，按时间顺序排列。"""
        return tuple(self._moves)

    @property
    def move_count(self) -> int:
        """总手数：真实对局手数加上沙盘手数。"""
        return self._base_move_count + len(self._moves)

    @property
    def current_player(self) -> Player:
        """沙盘当前应当落子的棋手。"""
        return self._current_player

    @property
    def last_move(self) -> Optional[Move]:
        """最后一手；优先是沙盘最后一手，没有时是真实对局最后一手。"""
        return self._last_move

    @property
    def winner(self) -> Optional[Player]:
        """沙盘中的胜者；尚未分出胜负时为 None。"""
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
        """沙盘对局是否已经结束。"""
        return self._winner is not None or self._is_draw

    @property
    def has_sandbox_moves(self) -> bool:
        """沙盘中是否存在推演落子。"""
        return bool(self._moves)

    # ------------------------------------------------------------------
    # 推演操作
    # ------------------------------------------------------------------
    def place(
        self, row: int, col: int, player: Optional[Player] = None
    ) -> bool:
        """在沙盘棋盘上落子。

        参数：
            row: 落子行号。
            col: 落子列号。
            player: 落子棋手；None 表示使用当前执子方。

        返回：
            落子成功返回 True；越界、位置被占用或沙盘已终局返回 False。
        """
        if player is None:
            player = self._current_player
        elif not isinstance(player, Player):
            raise ValueError("player 必须是 Player 枚举值或 None")

        if self.is_game_over:
            return False
        if not 0 <= row < BOARD_SIZE or not 0 <= col < BOARD_SIZE:
            return False
        if self._board[row][col] is not None:
            return False

        self._board[row][col] = player
        move = Move(row=row, col=col, player=player)
        self._moves.append(move)
        self._last_move = move

        winning_line = find_winning_line(self._board, row, col, player)
        if winning_line:
            self._winner = player
            self._winning_line = tuple(winning_line)
        elif self._count_stones() >= BOARD_CELL_COUNT:
            self._is_draw = True
        else:
            # 沙盘中用户轮流执子，自动切换到对方以便继续推演。
            self._current_player = player.opponent
        return True

    def undo(self) -> bool:
        """撤销最后一手沙盘落子，并把执子权交还该手棋手。

        返回：
            撤销成功返回 True；沙盘没有任何落子时返回 False。
        """
        if not self._moves:
            return False

        move = self._moves.pop()
        self._board[move.row][move.col] = None
        self._current_player = move.player
        if self._moves:
            self._last_move = self._moves[-1]
        else:
            self._last_move = self._base_last_move
        self._recompute_status()
        return True

    def reset(self) -> None:
        """恢复到真实局面的基线，丢弃全部沙盘推演。"""
        self._board = [list(row) for row in self._base_board]
        self._moves.clear()
        self._current_player = self._base_current_player
        self._last_move = self._base_last_move
        self._winner = None
        self._winning_line = ()
        self._is_draw = False

    def switch_player(self, player: Player) -> None:
        """切换沙盘当前的执子方。"""
        if not isinstance(player, Player):
            raise ValueError("player 必须是 Player 枚举值")
        self._current_player = player

    def to_view(self) -> BoardView:
        """把沙盘状态投影成棋盘只读快照。"""
        return BoardView(
            board=self.board,
            current_player=self.current_player,
            last_move=self.last_move,
            winner=self.winner,
            winning_line=self.winning_line,
            is_draw=self.is_draw,
            is_game_over=self.is_game_over,
            move_count=self.move_count,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _copy_board(board: BoardLike) -> tuple[tuple[Cell, ...], ...]:
        """校验棋盘尺寸并复制成不可变快照。"""
        rows = list(board)
        if len(rows) != BOARD_SIZE:
            raise ValueError(
                f"棋盘必须是 {BOARD_SIZE}x{BOARD_SIZE}，实际有 {len(rows)} 行"
            )
        copied: list[tuple[Cell, ...]] = []
        for index, row in enumerate(rows):
            cells = list(row)
            if len(cells) != BOARD_SIZE:
                raise ValueError(
                    f"棋盘必须是 {BOARD_SIZE}x{BOARD_SIZE}，"
                    f"第 {index} 行有 {len(cells)} 列"
                )
            copied.append(tuple(cells))
        return tuple(copied)

    def _count_stones(self) -> int:
        """统计棋盘上的棋子总数（含基线棋子）。"""
        return sum(1 for row in self._board for cell in row
                   if cell is not None)

    def _recompute_status(self) -> None:
        """按当前棋盘重新计算胜负与终局状态。

        只有"含有沙盘落子"的五连才计入沙盘胜利：真实局面（基线）被视为仍在
        进行中，因此初始化与 :meth:`reset` 之后各项终局状态都是清空的。
        """
        self._winner = None
        self._winning_line = ()
        self._is_draw = False
        if not self._moves:
            return

        sandbox_cells = {(move.row, move.col) for move in self._moves}
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                player = self._board[row][col]
                if player is None:
                    continue
                line = find_winning_line(self._board, row, col, player)
                if line and any(cell in sandbox_cells for cell in line):
                    self._winner = player
                    self._winning_line = tuple(line)
                    break
            if self._winner is not None:
                break

        if self._winner is None and self._count_stones() >= BOARD_CELL_COUNT:
            self._is_draw = True


__all__ = ["SandboxModel", "format_coordinate"]
