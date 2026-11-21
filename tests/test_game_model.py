"""五子棋规则层的单元测试。

运行方式（在项目根目录）：
    python -m unittest discover -s tests -v

这些测试不启动 Qt 窗口，因此既可在开发机上运行，也适合做打包前的快速回归。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 允许直接执行本文件，也兼容从项目根目录执行 unittest discover。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.shared.game_model import GameModel  # noqa: E402
from gomoku.shared.rules import Player, find_winning_line  # noqa: E402


class GameModelTests(unittest.TestCase):
    """覆盖胜负判定、轮换、悔棋、重置和平局等核心规则。"""

    def _play_alternating(
        self,
        black_moves: list[tuple[int, int]],
        white_moves: list[tuple[int, int]],
    ) -> GameModel:
        """按黑先白后的顺序交错执行落子，并返回完成后的模型。"""
        game = GameModel()
        for index, black_move in enumerate(black_moves):
            self.assertTrue(
                game.place_stone(*black_move),
                msg=f"黑棋第 {index + 1} 手应合法：{black_move}",
            )
            if index < len(white_moves):
                self.assertTrue(
                    game.place_stone(*white_moves[index]),
                    msg=f"白棋第 {index + 1} 手应合法：{white_moves[index]}",
                )
        return game

    def test_initial_state(self) -> None:
        """新开局应为空棋盘，黑棋先行。"""
        game = GameModel()

        self.assertEqual(len(game.board), 15)
        self.assertTrue(all(len(row) == 15 for row in game.board))
        self.assertTrue(all(cell is None for row in game.board for cell in row))
        self.assertEqual(game.current_player, Player.BLACK)
        self.assertEqual(game.move_count, 0)
        self.assertIsNone(game.last_move)
        self.assertIsNone(game.winner)
        self.assertFalse(game.is_draw)
        self.assertFalse(game.is_game_over)
        self.assertEqual(len(game.empty_cells()), 225)

    def test_players_alternate(self) -> None:
        """每次合法落子后轮到另一方，并记录最后一手。"""
        game = GameModel()

        self.assertTrue(game.place_stone(7, 7))
        self.assertEqual(game.current_player, Player.WHITE)
        self.assertEqual(game.move_count, 1)
        self.assertEqual((game.last_move.row, game.last_move.col), (7, 7))

        self.assertTrue(game.place_stone(7, 8))
        self.assertEqual(game.current_player, Player.BLACK)
        self.assertEqual(game.move_count, 2)

    def test_invalid_moves_are_rejected(self) -> None:
        """越界位置、已有棋子的位置都不能落子。"""
        game = GameModel()

        self.assertFalse(game.place_stone(-1, 0))
        self.assertFalse(game.place_stone(0, -1))
        self.assertFalse(game.place_stone(15, 0))
        self.assertFalse(game.place_stone(0, 15))
        self.assertEqual(game.move_count, 0)

        self.assertTrue(game.place_stone(7, 7))
        self.assertFalse(game.place_stone(7, 7))
        self.assertEqual(game.move_count, 1)
        self.assertEqual(game.current_player, Player.WHITE)

    def test_all_four_directions_can_win(self) -> None:
        """横、竖、主对角线和副对角线均能正确判定五连。"""
        cases = {
            "横向": (
                [(7, 3), (7, 4), (7, 5), (7, 6), (7, 7)],
                [(9, 3), (9, 4), (9, 5), (9, 6)],
            ),
            "纵向": (
                [(3, 7), (4, 7), (5, 7), (6, 7), (7, 7)],
                [(3, 9), (4, 9), (5, 9), (6, 9)],
            ),
            "主对角线": (
                [(3, 3), (4, 4), (5, 5), (6, 6), (7, 7)],
                [(0, 0), (0, 1), (0, 2), (0, 3)],
            ),
            "副对角线": (
                [(3, 11), (4, 10), (5, 9), (6, 8), (7, 7)],
                [(0, 0), (0, 1), (0, 2), (0, 3)],
            ),
        }

        for name, (black_moves, white_moves) in cases.items():
            with self.subTest(direction=name):
                game = self._play_alternating(black_moves, white_moves)

                self.assertEqual(game.winner, Player.BLACK)
                self.assertTrue(game.is_game_over)
                self.assertFalse(game.is_draw)
                self.assertEqual(game.current_player, Player.BLACK)
                self.assertEqual(len(game.winning_line), 5)
                self.assertEqual(
                    set(game.winning_line),
                    set(black_moves),
                )

                # 终局后任何一方都不能继续落子，状态保持不变。
                self.assertFalse(game.place_stone(0, 14))
                self.assertEqual(game.move_count, 9)

    def test_overline_counts_as_win(self) -> None:
        """休闲自由规则下，六连等长连也应判胜并记录完整连线。"""
        # 黑棋先在 3、4、5、7、8 列落子，此时没有五连；
        # 白棋的落子相互间隔；最后黑棋补上 6 列形成六连。
        black_moves = [(7, 3), (7, 4), (7, 5), (7, 7), (7, 8), (7, 6)]
        white_moves = [(0, 0), (0, 2), (0, 4), (0, 6), (0, 8)]

        game = self._play_alternating(black_moves, white_moves)

        self.assertEqual(game.winner, Player.BLACK)
        self.assertEqual(len(game.winning_line), 6)
        self.assertEqual(
            set(game.winning_line),
            {(7, 3), (7, 4), (7, 5), (7, 6), (7, 7), (7, 8)},
        )

    def test_undo_after_win_resumes_game(self) -> None:
        """悔棋会撤销胜利手，清除终局状态并恢复胜方的行动权。"""
        black_moves = [(7, 3), (7, 4), (7, 5), (7, 6), (7, 7)]
        white_moves = [(9, 3), (9, 4), (9, 5), (9, 6)]
        game = self._play_alternating(black_moves, white_moves)

        self.assertTrue(game.is_game_over)
        self.assertTrue(game.undo())

        self.assertFalse(game.is_game_over)
        self.assertIsNone(game.winner)
        self.assertFalse(game.is_draw)
        self.assertEqual(game.winning_line, ())
        self.assertEqual(game.move_count, 8)
        self.assertEqual(game.current_player, Player.BLACK)
        self.assertIsNone(game.board[7][7])
        self.assertEqual(
            (game.last_move.row, game.last_move.col, game.last_move.player),
            (9, 6, Player.WHITE),
        )

        # 重新在刚撤销的位置落子，应再次获胜。
        self.assertTrue(game.place_stone(7, 7))
        self.assertEqual(game.winner, Player.BLACK)

    def test_undo_when_board_is_empty(self) -> None:
        """空棋盘没有可撤销的棋步。"""
        game = GameModel()
        self.assertFalse(game.undo())

    def test_reset_clears_everything(self) -> None:
        """重新开局应恢复空棋盘、黑棋先行和未结束状态。"""
        game = GameModel()
        self.assertTrue(game.place_stone(7, 7))
        self.assertTrue(game.place_stone(7, 8))
        game.reset()

        self.assertEqual(game.move_count, 0)
        self.assertIsNone(game.last_move)
        self.assertIsNone(game.winner)
        self.assertFalse(game.is_game_over)
        self.assertFalse(game.is_draw)
        self.assertEqual(game.current_player, Player.BLACK)
        self.assertTrue(all(cell is None for row in game.board for cell in row))
        self.assertFalse(game.undo())

    def test_full_board_without_five_is_draw(self) -> None:
        """棋盘填满且没有五连时应判定平局，并支持悔棋继续。"""
        # 以周期 4 的颜色布局填满棋盘，可保证任何方向都没有五连。
        # 该布局黑色 113 格、白色 112 格，正好符合黑棋先行的轮流顺序。
        black_cells: list[tuple[int, int]] = []
        white_cells: list[tuple[int, int]] = []
        for row in range(15):
            for col in range(15):
                if (row + 2 * col) % 4 < 2:
                    black_cells.append((row, col))
                else:
                    white_cells.append((row, col))

        self.assertEqual(len(black_cells), 113)
        self.assertEqual(len(white_cells), 112)

        game = GameModel()
        for index, white_move in enumerate(white_cells):
            self.assertTrue(game.place_stone(*black_cells[index]))
            self.assertTrue(game.place_stone(*white_move))
        self.assertTrue(game.place_stone(*black_cells[-1]))

        self.assertTrue(game.is_draw)
        self.assertIsNone(game.winner)
        self.assertTrue(game.is_game_over)
        self.assertEqual(game.move_count, 225)
        self.assertEqual(game.empty_cells(), [])
        self.assertFalse(game.place_stone(0, 0))

        # 悔棋后应移除最后一颗黑棋，回到黑棋行动且对局继续。
        self.assertTrue(game.undo())
        self.assertFalse(game.is_draw)
        self.assertFalse(game.is_game_over)
        self.assertEqual(game.move_count, 224)
        self.assertEqual(game.current_player, Player.BLACK)
        self.assertIsNone(game.board[black_cells[-1][0]][black_cells[-1][1]])

    def test_player_labels_and_opponents(self) -> None:
        """棋手中文名和对手关系应正确。"""
        self.assertEqual(Player.BLACK.label, "黑棋")
        self.assertEqual(Player.WHITE.label, "白棋")
        self.assertEqual(Player.BLACK.opponent, Player.WHITE)
        self.assertEqual(Player.WHITE.opponent, Player.BLACK)

    def test_shared_winning_line_function(self) -> None:
        """供 AI 复用的纯胜线函数应保持正式对局规则。"""
        board: list[list[Player | None]] = [
            [None for _ in range(15)] for _ in range(15)
        ]
        for col in range(3, 8):
            board[7][col] = Player.BLACK

        line = find_winning_line(board, 7, 7, Player.BLACK)
        self.assertEqual(
            line,
            [(7, 3), (7, 4), (7, 5), (7, 6), (7, 7)],
        )
        self.assertEqual(find_winning_line(board, 7, 8, Player.BLACK), [])
        self.assertEqual(find_winning_line(board, 7, 7, Player.WHITE), [])
        self.assertEqual(find_winning_line(board, -1, 0, Player.BLACK), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
