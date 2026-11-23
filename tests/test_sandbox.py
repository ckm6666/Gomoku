"""沙盘推演（sandbox）模型的纯逻辑单元测试。

沙盘是真实联机局面的本地分叉：用户在沙盘上同时执黑执白自由推演，沙盘完全
运行在客户端内存中，绝不触碰网络，也绝不修改真实对局的权威状态。

本文件只覆盖 :mod:`gomoku.client.sandbox` 中的规则与状态推导，不依赖 PySide6，
因此可以直接在无界面的单元测试中导入使用。覆盖三件事：

* 沙盘与真实局面彻底隔离（基线被复制，绝不回写真实棋盘）；
* 落子/撤销/重置/切换执子方的状态推导正确；
* 坐标格式化与非法输入的防御。
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

from gomoku.client.board_view import BoardView  # noqa: E402
from gomoku.client.sandbox import (  # noqa: E402
    SandboxModel,
    format_coordinate,
)
from gomoku.shared.rules import BOARD_SIZE, Cell, Move, Player  # noqa: E402


def make_board(
    stones: dict[tuple[int, int], Player] | None = None,
) -> tuple[tuple[Cell, ...], ...]:
    """按坐标字典构造一张 15×15 的只读棋盘。"""
    board: list[list[Cell]] = [
        [None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
    ]
    for (row, col), player in (stones or {}).items():
        board[row][col] = player
    return tuple(tuple(row) for row in board)


def make_view(
    stones: dict[tuple[int, int], Player] | None = None,
    *,
    current_player: Player = Player.BLACK,
    move_count: int = 0,
    last_move: Move | None = None,
) -> BoardView:
    """构造一张符合协议的棋盘只读快照。"""
    return BoardView(
        board=make_board(stones),
        current_player=current_player,
        last_move=last_move,
        winner=None,
        winning_line=(),
        is_draw=False,
        is_game_over=False,
        move_count=move_count,
    )


class SandboxBaseIsolationTests(unittest.TestCase):
    """沙盘不能影响真实局面。"""

    def test_base_board_is_copied_and_never_mutated(self) -> None:
        """沙盘落子之后，真实棋盘必须逐格保持原样。"""
        view = make_view(
            {(7, 7): Player.BLACK, (7, 8): Player.WHITE},
            current_player=Player.BLACK,
            move_count=2,
        )
        before = repr(view.board)

        model = SandboxModel.from_view(view)
        self.assertTrue(model.place(0, 0))

        # 真实快照的棋盘逐格不变。
        self.assertEqual(repr(view.board), before)
        self.assertIsNone(view.board[0][0])
        self.assertEqual(view.board[7][7], Player.BLACK)
        self.assertEqual(view.board[7][8], Player.WHITE)
        # 真实快照的其他字段也不该被沙盘改动。
        self.assertEqual(view.move_count, 2)
        self.assertEqual(view.current_player, Player.BLACK)

        # 模型自己的基线同样没有被沙盘落子污染。
        self.assertEqual(model.base_move_count, 2)
        model.reset()
        self.assertEqual(model.board, view.board)
        self.assertEqual(model.current_player, Player.BLACK)
        self.assertEqual(model.move_count, 2)


class SandboxPlaceTests(unittest.TestCase):
    """沙盘落子。"""

    def test_place_adds_stone_and_switches_side(self) -> None:
        """落子后棋盘多一颗子、棋谱多一手，并且自动轮到对方。"""
        model = SandboxModel.from_view(
            make_view(current_player=Player.BLACK, move_count=2)
        )
        self.assertTrue(model.place(7, 7))

        self.assertEqual(model.board[7][7], Player.BLACK)
        self.assertEqual(len(model.moves), 1)
        self.assertEqual(model.moves[0].row, 7)
        self.assertEqual(model.moves[0].col, 7)
        self.assertEqual(model.moves[0].player, Player.BLACK)
        # 沙盘里用户轮流执子，落子后自动换到对方。
        self.assertEqual(model.current_player, Player.WHITE)
        self.assertEqual(model.move_count, model.base_move_count + 1)
        self.assertTrue(model.has_sandbox_moves)
        # 最后一手指向刚落下的一手。
        self.assertEqual(model.last_move, model.moves[0])

    def test_place_rejects_occupied_and_out_of_bounds(self) -> None:
        """占位与越界都要被拒绝，且不改变任何状态。"""
        model = SandboxModel.from_view(
            make_view({(7, 7): Player.BLACK}, move_count=1)
        )
        self.assertFalse(model.place(7, 7))
        self.assertFalse(model.place(-1, 0))
        self.assertFalse(model.place(0, -1))
        self.assertFalse(model.place(BOARD_SIZE, 0))
        self.assertFalse(model.place(0, BOARD_SIZE))

        self.assertEqual(len(model.moves), 0)
        self.assertEqual(model.move_count, 1)
        self.assertEqual(model.current_player, Player.BLACK)
        self.assertFalse(model.has_sandbox_moves)

    def test_place_rejects_after_game_over(self) -> None:
        """沙盘里已经分出胜负后就不能再落子了。"""
        model = SandboxModel.from_view(make_view(current_player=Player.BLACK))
        for col in range(3, 8):
            self.assertTrue(model.place(7, col, Player.BLACK))

        self.assertTrue(model.is_game_over)
        self.assertFalse(model.place(0, 0))
        self.assertEqual(len(model.moves), 5)
        self.assertEqual(model.move_count, 5)

    def test_place_rejects_invalid_player(self) -> None:
        """执子方参数必须是 Player 枚举值。"""
        model = SandboxModel.from_view(make_view(current_player=Player.BLACK))
        with self.assertRaises(ValueError):
            model.place(0, 0, "black")  # type: ignore[arg-type]


class SandboxUndoTests(unittest.TestCase):
    """沙盘撤销。"""

    def test_undo_restores_previous_state_and_returns_the_side(self) -> None:
        """撤销要拿走最后一手，并把执子权交还给该手棋手。"""
        model = SandboxModel.from_view(
            make_view(current_player=Player.BLACK, move_count=2)
        )
        self.assertTrue(model.place(7, 7))
        self.assertEqual(model.current_player, Player.WHITE)

        self.assertTrue(model.undo())
        self.assertIsNone(model.board[7][7])
        self.assertEqual(model.current_player, Player.BLACK)
        self.assertFalse(model.has_sandbox_moves)
        self.assertEqual(len(model.moves), 0)
        self.assertEqual(model.move_count, model.base_move_count)

        # 没有可撤销的落子时撤销必须失败。
        self.assertFalse(model.undo())

    def test_undo_reopens_a_finished_game(self) -> None:
        """五连之后撤销一手，棋局要重新变成“未结束”。"""
        model = SandboxModel.from_view(make_view(current_player=Player.BLACK))
        for col in range(3, 8):
            self.assertTrue(model.place(7, col, Player.BLACK))

        self.assertTrue(model.is_game_over)
        self.assertEqual(model.winner, Player.BLACK)

        self.assertTrue(model.undo())
        self.assertFalse(model.is_game_over)
        self.assertIsNone(model.winner)
        self.assertEqual(model.winning_line, ())
        self.assertFalse(model.is_draw)


class SandboxResetTests(unittest.TestCase):
    """沙盘重置。"""

    def test_reset_restores_the_base_position(self) -> None:
        """重置后棋盘、执子方与手数都要回到真实局面。"""
        view = make_view(
            {(7, 7): Player.BLACK},
            current_player=Player.WHITE,
            move_count=1,
        )
        model = SandboxModel.from_view(view)
        self.assertTrue(model.place(0, 0))
        self.assertTrue(model.place(1, 1))
        self.assertEqual(len(model.moves), 2)

        model.reset()
        self.assertEqual(model.board, view.board)
        self.assertEqual(model.current_player, Player.WHITE)
        self.assertEqual(model.move_count, 1)
        self.assertFalse(model.has_sandbox_moves)
        self.assertIsNone(model.winner)
        self.assertEqual(model.winning_line, ())
        self.assertFalse(model.is_draw)
        self.assertFalse(model.is_game_over)


class SandboxWinTests(unittest.TestCase):
    """沙盘中的胜负判定。"""

    def test_five_in_a_row_sets_winner_and_winning_line(self) -> None:
        """五连要判出胜者，并给出完整的胜利连线。"""
        model = SandboxModel.from_view(make_view(current_player=Player.BLACK))
        for col in range(3, 8):
            self.assertTrue(model.place(7, col, Player.BLACK))

        self.assertEqual(model.winner, Player.BLACK)
        self.assertEqual(len(model.winning_line), 5)
        self.assertIn((7, 7), model.winning_line)
        self.assertTrue(model.is_game_over)
        self.assertFalse(model.is_draw)


class SandboxSideSwitchTests(unittest.TestCase):
    """沙盘里切换执子方。"""

    def test_switch_player_changes_the_side(self) -> None:
        """切换执子方后落子要用新的颜色。"""
        model = SandboxModel.from_view(make_view(current_player=Player.BLACK))
        model.switch_player(Player.WHITE)
        self.assertEqual(model.current_player, Player.WHITE)

        self.assertTrue(model.place(7, 7))
        self.assertEqual(model.board[7][7], Player.WHITE)

    def test_switch_player_rejects_non_player(self) -> None:
        """非 Player 的执子方必须报错。"""
        model = SandboxModel.from_view(make_view(current_player=Player.BLACK))
        with self.assertRaises(ValueError):
            model.switch_player("white")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            model.switch_player(None)  # type: ignore[arg-type]
        # 报错后执子方保持不变。
        self.assertEqual(model.current_player, Player.BLACK)


class SandboxViewProjectionTests(unittest.TestCase):
    """沙盘状态投影成棋盘快照。"""

    def test_to_view_projects_the_sandbox_state(self) -> None:
        """投影出来的快照必须和沙盘模型逐项一致。"""
        model = SandboxModel.from_view(
            make_view(current_player=Player.BLACK, move_count=3)
        )
        self.assertTrue(model.place(7, 7))
        view = model.to_view()

        self.assertEqual(view.board, model.board)
        self.assertEqual(view.current_player, model.current_player)
        self.assertEqual(view.last_move, model.last_move)
        self.assertIsNone(view.winner)
        self.assertEqual(view.winning_line, ())
        self.assertEqual(view.move_count, model.move_count)
        self.assertFalse(view.is_game_over)

        # 终局之后投影里也要带上胜者与胜利连线。
        for col in range(3, 8):
            self.assertTrue(model.place(6, col, Player.BLACK))
        finished = model.to_view()
        self.assertEqual(finished.winner, Player.BLACK)
        self.assertEqual(len(finished.winning_line), 5)
        self.assertTrue(finished.is_game_over)


class SandboxCoordinateTests(unittest.TestCase):
    """棋盘记号格式化。"""

    def test_format_coordinate(self) -> None:
        """坐标要格式化成“A1”这种记号。"""
        self.assertEqual(format_coordinate(7, 7), "H8")
        self.assertEqual(format_coordinate(0, 0), "A1")
        self.assertEqual(format_coordinate(14, 14), "O15")
        self.assertEqual(format_coordinate(0, 14), "O1")
        self.assertEqual(format_coordinate(14, 0), "A15")


class SandboxValidationTests(unittest.TestCase):
    """沙盘模型的输入校验。"""

    def test_invalid_board_size_raises_value_error(self) -> None:
        """棋盘必须是 15×15，否则一律报错。"""
        with self.assertRaises(ValueError):
            SandboxModel(
                board=[[None for _ in range(14)] for _ in range(14)],
                current_player=Player.BLACK,
            )
        with self.assertRaises(ValueError):
            # 行数对、列数不对同样要报错。
            SandboxModel(
                board=[
                    [None for _ in range(BOARD_SIZE)]
                    for _ in range(BOARD_SIZE - 1)
                ],
                current_player=Player.BLACK,
            )

    def test_invalid_current_player_raises_value_error(self) -> None:
        """执子方必须是 Player 枚举值。"""
        with self.assertRaises(ValueError):
            SandboxModel(
                board=make_board(),
                current_player="black",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
