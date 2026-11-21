"""自研五子棋 AI 引擎的规则与性能回归测试。

测试不启动 Qt，直接调用纯 Python 引擎，因此可以在命令行快速运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client.ai_engine import (  # noqa: E402
    DIFFICULTY_CONFIGS,
    HINT_CONFIG,
    AIDifficulty,
    GomokuAI,
)
from gomoku.client.opening_book import (  # noqa: E402
    load_opening_book,
)
from gomoku.client.threat_search import (  # noqa: E402
    SearchBudget,
    ThreatSearchCancelled,
    find_vcf_win,
    find_vct_win,
    verify_double_threat,
    verify_forced_line,
)
from gomoku.client.transposition import (  # noqa: E402
    Bound,
    TranspositionTable,
)
from gomoku.client.zobrist import apply_move, compute_hash  # noqa: E402
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
        """四档难度必须有递增深度及不同评估/搜索参数。"""
        easy = DIFFICULTY_CONFIGS[AIDifficulty.EASY]
        medium = DIFFICULTY_CONFIGS[AIDifficulty.MEDIUM]
        hard = DIFFICULTY_CONFIGS[AIDifficulty.HARD]
        master = DIFFICULTY_CONFIGS[AIDifficulty.MASTER]

        self.assertLess(easy.max_depth, medium.max_depth)
        self.assertLess(medium.max_depth, hard.max_depth)
        self.assertLess(hard.max_depth, master.max_depth)
        self.assertGreater(easy.time_limit, 0)
        self.assertGreater(medium.inner_candidates, 0)
        self.assertGreater(hard.root_candidates, medium.root_candidates)
        self.assertTrue(hard.rich_evaluation)
        self.assertFalse(medium.rich_evaluation)
        self.assertGreaterEqual(easy.random_top_k, 0)
        self.assertGreaterEqual(master.time_limit, 1.5)
        self.assertLessEqual(master.time_limit, 2.5)
        self.assertTrue(master.use_opening_book)
        self.assertTrue(master.use_vcf)
        self.assertTrue(master.use_transposition_table)
        self.assertTrue(master.rich_evaluation)
        self.assertTrue(HINT_CONFIG.use_vcf)
        self.assertLess(HINT_CONFIG.time_limit, master.time_limit)

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
                budget = DIFFICULTY_CONFIGS[difficulty].time_limit
                self.assertLess(
                    elapsed,
                    max(4.0, budget + 1.5),
                    ai.last_stats,
                )

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


def vcf_double_four_board() -> list[list[Player | None]]:
    """黑棋在 (7,7) 可同时形成横、竖两个四，属于一手双四。"""
    return make_board(
        black_cells=[(7, 4), (7, 5), (7, 6), (4, 7), (5, 7), (6, 7)],
        white_cells=[(7, 3), (3, 7)],
    )


def double_three_board() -> list[list[Player | None]]:
    """黑棋在 (7,7) 同时形成横、竖两个活三，属于双威胁。"""
    return make_board(
        black_cells=[(7, 5), (7, 6), (5, 7), (6, 7)],
        white_cells=[(0, 0), (0, 14), (14, 0), (14, 14)],
    )


class ThreatSearchTests(unittest.TestCase):
    """覆盖 VCF/VCT 的证明、否定与取消路径。"""

    def test_vcf_finds_double_four(self) -> None:
        snap = snapshot(vcf_double_four_board())
        budget = SearchBudget(
            deadline=time.monotonic() + 2.0,
            node_limit=100_000,
        )
        line = find_vcf_win(snap, Player.BLACK, budget=budget)
        self.assertIsNotNone(line)
        self.assertEqual(line.moves[0], (7, 7))
        self.assertTrue(
            verify_forced_line(snap, line.moves, Player.BLACK)
        )

    def test_vcf_quiet_position_returns_none(self) -> None:
        snap = snapshot(no_immediate_threat_board())
        budget = SearchBudget(
            deadline=time.monotonic() + 1.0,
            node_limit=100_000,
        )
        self.assertIsNone(find_vcf_win(snap, Player.BLACK, budget=budget))

    def test_vcf_respects_cancel_event(self) -> None:
        snap = snapshot(vcf_double_four_board())
        cancel_event = threading.Event()
        cancel_event.set()
        budget = SearchBudget(
            deadline=time.monotonic() + 1.0,
            node_limit=10_000,
            cancel_event=cancel_event,
        )
        with self.assertRaises(ThreatSearchCancelled):
            find_vcf_win(snap, Player.BLACK, budget=budget)

    def test_vct_finds_double_three(self) -> None:
        snap = snapshot(double_three_board())
        budget = SearchBudget(
            deadline=time.monotonic() + 2.0,
            node_limit=200_000,
        )
        line = find_vct_win(snap, Player.BLACK, budget=budget)
        self.assertIsNotNone(line)
        self.assertEqual(line.moves[0], (7, 7))
        self.assertTrue(
            verify_double_threat(snap, line.moves[0], Player.BLACK)
        )

    def test_vct_quiet_position_returns_none(self) -> None:
        snap = snapshot(no_immediate_threat_board())
        budget = SearchBudget(
            deadline=time.monotonic() + 1.0,
            node_limit=200_000,
        )
        self.assertIsNone(find_vct_win(snap, Player.BLACK, budget=budget))

    def test_forced_line_verifier_rejects_invalid_line(self) -> None:
        snap = snapshot(vcf_double_four_board())
        self.assertFalse(
            verify_forced_line(snap, [(0, 0)], Player.BLACK)
        )


class ZobristAndTranspositionTests(unittest.TestCase):
    """覆盖哈希增量更新与置换表边界/容量语义。"""

    def test_incremental_hash_matches_full_recompute(self) -> None:
        snap = snapshot(no_immediate_threat_board())
        mutable = [list(row) for row in snap]
        value = compute_hash(mutable, Player.BLACK)
        side = Player.BLACK
        moves = [
            ((7, 7), Player.BLACK),
            ((8, 7), Player.WHITE),
            ((6, 8), Player.BLACK),
        ]

        for (row, col), player in moves:
            self.assertIs(player, side)
            mutable[row][col] = player
            value = apply_move(value, player, row, col)
            side = side.opponent
            self.assertEqual(value, compute_hash(mutable, side))

        for (row, col), player in reversed(moves):
            value = apply_move(value, player, row, col)
            mutable[row][col] = None
            side = side.opponent
            self.assertEqual(value, compute_hash(mutable, side))

    def test_transposition_table_keeps_deeper_result(self) -> None:
        table = TranspositionTable(max_entries=10)
        table.store(1, 3, 100, Bound.EXACT, (0, 0))
        table.store(1, 1, 50, Bound.EXACT, (1, 1))
        entry = table.probe(1)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.depth, 3)
        self.assertEqual(entry.best_move, (0, 0))

    def test_transposition_table_respects_capacity(self) -> None:
        table = TranspositionTable(max_entries=2)
        for key in range(5):
            table.store(key, 1, key, Bound.EXACT, (0, key))
        self.assertLessEqual(len(table), 2)

        table.new_generation()
        table.store(99, 2, 999, Bound.LOWER, (7, 7))
        self.assertLessEqual(len(table), 2)
        self.assertIsNotNone(table.probe(99))

    def test_transposition_table_does_not_change_score(self) -> None:
        snap = snapshot(no_immediate_threat_board())
        base = replace(
            DIFFICULTY_CONFIGS[AIDifficulty.MEDIUM],
            time_limit=0.4,
            node_limit=8_000,
        )
        with_tt = replace(
            base,
            use_transposition_table=True,
            tt_max_entries=10_000,
        )
        without = GomokuAI(seed=4).analyze(
            snap,
            Player.BLACK,
            AIDifficulty.MEDIUM,
            top_n=3,
            config_override=base,
        )
        enabled = GomokuAI(seed=4).analyze(
            snap,
            Player.BLACK,
            AIDifficulty.MEDIUM,
            top_n=3,
            config_override=with_tt,
        )
        self.assertEqual(without.best_score, enabled.best_score)
        self.assertEqual(without.best_move, enabled.best_move)


class OpeningBookTests(unittest.TestCase):
    """覆盖开局库加载、对称查询与库外回落。"""

    def test_book_loads_and_returns_center_reply(self) -> None:
        book = load_opening_book()
        self.assertGreater(len(book), 0)
        board = make_board(black_cells=[(7, 7)])
        snap = snapshot(board)
        reply = book.lookup([(7, 7)], snap, random.Random(7))
        self.assertIsNotNone(reply)
        self.assertIsNone(snap[reply[0]][reply[1]])
        self.assertIn(
            reply,
            {
                (6, 6),
                (6, 8),
                (8, 6),
                (8, 8),
                (6, 7),
                (7, 6),
                (7, 8),
                (8, 7),
            },
        )

    def test_book_symmetry_lookup(self) -> None:
        book = load_opening_book()
        board = make_board(
            black_cells=[(7, 7)], white_cells=[(8, 8)]
        )
        reply = book.lookup(
            [(7, 7), (8, 8)],
            snapshot(board),
            random.Random(1),
        )
        self.assertIsNotNone(reply)
        self.assertIsNone(board[reply[0]][reply[1]])

    def test_book_out_of_book_returns_none(self) -> None:
        book = load_opening_book()
        board = make_board(
            black_cells=[(0, 0)], white_cells=[(14, 14)]
        )
        self.assertIsNone(
            book.lookup(
                [(0, 0), (14, 14)],
                snapshot(board),
                random.Random(1),
            )
        )

    def test_missing_or_corrupt_book_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(len(load_opening_book(missing)), 0)

            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(len(load_opening_book(corrupt)), 0)

    def test_master_uses_opening_book(self) -> None:
        book = load_opening_book()
        board = make_board(black_cells=[(7, 7)])
        ai = GomokuAI(
            seed=1,
            opening_book=book,
            move_history=[(7, 7)],
        )
        move = ai.find_move(
            snapshot(board), Player.WHITE, AIDifficulty.MASTER
        )
        self.assertIsNotNone(move)
        self.assertIsNone(board[move[0]][move[1]])
        self.assertEqual(ai.last_stats.source, "book")


class AnalysisAndMasterTests(unittest.TestCase):
    """覆盖分析 API 与大师档的强制战术。"""

    def test_analyze_forced_win_returns_vcf_source(self) -> None:
        snap = snapshot(vcf_double_four_board())
        analysis = GomokuAI(seed=1).analyze(
            snap,
            Player.BLACK,
            AIDifficulty.MASTER,
            top_n=3,
            config_override=HINT_CONFIG,
        )
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis.best_move, (7, 7))
        self.assertEqual(analysis.source, "vcf")
        self.assertTrue(analysis.candidates)
        self.assertEqual(analysis.candidates[0].move, (7, 7))

    def test_analyze_quiet_position_is_legal_and_sorted(self) -> None:
        snap = snapshot(no_immediate_threat_board())
        config = replace(
            DIFFICULTY_CONFIGS[AIDifficulty.HARD],
            time_limit=0.4,
            node_limit=8_000,
        )
        analysis = GomokuAI(seed=2).analyze(
            snap,
            Player.BLACK,
            AIDifficulty.HARD,
            top_n=3,
            config_override=config,
        )
        self.assertIsNotNone(analysis)
        self.assertIsNotNone(analysis.best_move)
        row, col = analysis.best_move
        self.assertIsNone(snap[row][col])
        self.assertLessEqual(len(analysis.candidates), 3)
        scores = [candidate.score for candidate in analysis.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_analyze_cancel_returns_cancelled(self) -> None:
        snap = snapshot(no_immediate_threat_board())
        cancel_event = threading.Event()
        cancel_event.set()
        analysis = GomokuAI(seed=1).analyze(
            snap,
            Player.BLACK,
            AIDifficulty.MASTER,
            cancel_event=cancel_event,
        )
        self.assertIsNotNone(analysis)
        self.assertTrue(analysis.cancelled)
        self.assertIsNone(analysis.best_move)

    def test_find_move_and_analyze_agree_for_easy(self) -> None:
        snap = snapshot(no_immediate_threat_board())
        move = GomokuAI(seed=123).find_move(
            snap, Player.WHITE, AIDifficulty.EASY
        )
        analysis = GomokuAI(seed=123).analyze(
            snap,
            Player.WHITE,
            AIDifficulty.EASY,
            top_n=1,
        )
        self.assertIsNotNone(analysis)
        self.assertEqual(move, analysis.best_move)

    def test_master_finds_vcf_and_vct(self) -> None:
        ai = GomokuAI(seed=1)
        self.assertEqual(
            ai.find_move(
                snapshot(vcf_double_four_board()),
                Player.BLACK,
                AIDifficulty.MASTER,
            ),
            (7, 7),
        )
        self.assertEqual(ai.last_stats.source, "vcf")

        ai = GomokuAI(seed=1)
        self.assertEqual(
            ai.find_move(
                snapshot(double_three_board()),
                Player.BLACK,
                AIDifficulty.MASTER,
            ),
            (7, 7),
        )
        self.assertEqual(ai.last_stats.source, "vct")

    def test_master_blocks_immediate_loss(self) -> None:
        board = make_board(
            black_cells=[(0, 0), (2, 2), (4, 4)],
            white_cells=[(8, 3), (8, 4), (8, 5), (8, 6)],
        )
        move = GomokuAI(seed=5).find_move(
            snapshot(board), Player.BLACK, AIDifficulty.MASTER
        )
        self.assertIn(move, {(8, 2), (8, 7)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
