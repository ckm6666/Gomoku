"""自研五子棋 AI 引擎的规则与性能回归测试。

测试不启动 Qt，直接调用纯 Python 引擎，因此可以在命令行快速运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client.ai_engine import (  # noqa: E402
    DIFFICULTY_CONFIGS,
    AIDifficulty,
    GomokuAI,
)
from gomoku.shared.rules import Player, find_winning_line  # noqa: E402

BOARD_SIZE = 15
EMPTY = None


def make_board(
    black_cells: list[tuple[int, int]] | None = None,
    white_cells: list[tuple[int, int]] | None = None,
) -> list[list[Player | None]]:
    """构造指定棋子位置的空棋盘。"""
    board: list[list[Player | None]] = [
        [EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
    ]
    for row, col in black_cells or []:
        board[row][col] = Player.BLACK
    for row, col in white_cells or []:
        board[row][col] = Player.WHITE
    return board


def snapshot(board: list[list[Player | None]]) -> tuple[tuple[Player | None, ...], ...]:
    """将测试棋盘转换为 AI 接口使用的只读快照。"""
    return tuple(tuple(row) for row in board)


def move_wins(
    board: list[list[Player | None]],
    move: tuple[int, int],
    player: Player,
) -> bool:
    """在副本上落子并验证该手是否成五。"""
    row, col = move
    board[row][col] = player
    return bool(find_winning_line(board, row, col, player))


def no_immediate_threat_board() -> list[list[Player | None]]:
    """返回一个没有双方立即成五点的代表性中盘布局。

    该布局用于触发真正的搜索而非必防/必胜短路，并用于粗略性能回归。
    它不是某个真实对局的历史局面，但棋盘状态本身合法。
    """
    return make_board(
        black_cells=[
            (8, 2),
            (8, 14),
            (6, 11),
            (13, 11),
            (7, 1),
            (6, 4),
            (13, 12),
            (5, 1),
        ],
        white_cells=[
            (0, 7),
            (1, 14),
            (3, 10),
            (14, 13),
            (7, 5),
            (10, 7),
            (10, 10),
            (1, 5),
        ],
    )


class AIEngineTests(unittest.TestCase):
    """覆盖首手、战术、合法性、难度参数、取消与耗时。"""

    def test_empty_board_returns_center(self) -> None:
        """空棋盘上三档 AI 都应以天元作为首手。"""
        board = make_board()
        snap = snapshot(board)

        for difficulty in AIDifficulty:
            with self.subTest(difficulty=difficulty.label):
                ai = GomokuAI(seed=1)
                move = ai.find_move(snap, Player.BLACK, difficulty)
                self.assertEqual(move, (7, 7))
                self.assertEqual(snapshot(board), snap)
                self.assertIsNotNone(ai.last_stats)

    def test_difficulty_configs_differ(self) -> None:
        """三档难度必须有递增深度及不同评估/随机参数。"""
        easy = DIFFICULTY_CONFIGS[AIDifficulty.EASY]
        medium = DIFFICULTY_CONFIGS[AIDifficulty.MEDIUM]
        hard = DIFFICULTY_CONFIGS[AIDifficulty.HARD]

        self.assertLess(easy.max_depth, medium.max_depth)
        self.assertLess(medium.max_depth, hard.max_depth)
        self.assertGreater(easy.time_limit, 0)
        self.assertGreater(medium.inner_candidates, 0)
        self.assertGreater(hard.root_candidates, medium.root_candidates)
        self.assertTrue(hard.rich_evaluation)
        self.assertFalse(medium.rich_evaluation)
        self.assertGreaterEqual(easy.random_top_k, 0)

    def test_takes_immediate_win_for_both_colors(self) -> None:
        """三档 AI 都能发现并执行自己的立即五连。"""
        for difficulty in AIDifficulty:
            for ai_player in (Player.BLACK, Player.WHITE):
                with self.subTest(difficulty=difficulty.label, player=ai_player.label):
                    if ai_player is Player.BLACK:
                        black_cells = [(7, 3), (7, 4), (7, 5), (7, 6)]
                        white_cells = [(10, 3), (10, 4), (12, 6)]
                    else:
                        black_cells = [(2, 3), (2, 4), (4, 6)]
                        white_cells = [(8, 3), (8, 4), (8, 5), (8, 6)]
                    board = make_board(black_cells, white_cells)
                    snap = snapshot(board)

                    ai = GomokuAI(seed=3)
                    move = ai.find_move(snap, ai_player, difficulty)
                    self.assertIsNotNone(move)
                    self.assertEqual(snapshot(board), snap)
                    self.assertIsNone(board[move[0]][move[1]])
                    self.assertTrue(move_wins(board, move, ai_player))

    def test_blocks_immediate_loss(self) -> None:
        """三档 AI 在无己方立即胜手时，必须封堵对手的四连。"""
        # 黑棋只有散落的 3 子，完全没有立即成五的可能；
        # 白棋在横线 8 上已有 4 子，下一手即可获胜。
        board = make_board(
            black_cells=[(0, 0), (2, 2), (4, 4)],
            white_cells=[(8, 3), (8, 4), (8, 5), (8, 6)],
        )
        snap = snapshot(board)

        for difficulty in AIDifficulty:
            with self.subTest(difficulty=difficulty.label):
                ai = GomokuAI(seed=5)
                move = ai.find_move(snap, Player.BLACK, difficulty)
                self.assertIsNotNone(move)
                self.assertIn(move, {(8, 2), (8, 7)})
                self.assertEqual(snapshot(board), snap)

    def test_all_levels_return_legal_move_without_mutation(self) -> None:
        """三档 AI 均须返回界内空点，且不修改调用方快照。"""
        board = no_immediate_threat_board()
        snap = snapshot(board)

        for difficulty in AIDifficulty:
            with self.subTest(difficulty=difficulty.label):
                ai = GomokuAI(seed=6)
                start = time.perf_counter()
                move = ai.find_move(snap, Player.BLACK, difficulty)
                elapsed = time.perf_counter() - start

                self.assertIsNotNone(move)
                self.assertEqual(snapshot(board), snap)
                row, col = move
                self.assertTrue(0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE)
                self.assertIsNone(board[row][col])
                # 硬编码具体最佳手会限制算法优化；只对总预算做宽松回归。
                self.assertLess(elapsed, 3.0, ai.last_stats)

    def test_cancel_event_is_respected(self) -> None:
        """调用前已取消搜索时，引擎应快速返回 None 且标记取消。"""
        board = no_immediate_threat_board()
        snap = snapshot(board)
        cancel_event = threading.Event()
        cancel_event.set()

        for difficulty in AIDifficulty:
            with self.subTest(difficulty=difficulty.label):
                ai = GomokuAI(seed=7)
                start = time.perf_counter()
                move = ai.find_move(
                    snap,
                    Player.BLACK,
                    difficulty,
                    cancel_event=cancel_event,
                )
                elapsed = time.perf_counter() - start
                self.assertIsNone(move)
                self.assertLess(elapsed, 0.1)
                self.assertIsNotNone(ai.last_stats)
                self.assertTrue(ai.last_stats.cancelled)

    def test_seed_makes_easy_move_reproducible(self) -> None:
        """简单难度注入相同随机种子时，重复调用应得到相同着法。"""
        board = no_immediate_threat_board()
        snap = snapshot(board)
        first = GomokuAI(seed=123).find_move(
            snap, Player.WHITE, AIDifficulty.EASY
        )
        second = GomokuAI(seed=123).find_move(
            snap, Player.WHITE, AIDifficulty.EASY
        )
        self.assertEqual(first, second)

    def test_full_board_returns_none(self) -> None:
        """满盘没有可落位置时应返回 None，而不是越界索引。"""
        board = make_board()
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                board[row][col] = Player.BLACK if (row + col) % 2 == 0 else Player.WHITE
        for difficulty in AIDifficulty:
            with self.subTest(difficulty=difficulty.label):
                self.assertIsNone(
                    GomokuAI(seed=8).find_move(
                        snapshot(board), Player.BLACK, difficulty
                    )
                )

    def test_invalid_board_shape_is_rejected(self) -> None:
        """非法棋盘尺寸不应导致异常或越界访问。"""
        bad_row = make_board()[:14]
        invalid = tuple(tuple(row) for row in bad_row)
        self.assertIsNone(
            GomokuAI(seed=9).find_move(invalid, Player.BLACK, AIDifficulty.EASY)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
