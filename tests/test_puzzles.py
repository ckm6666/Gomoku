"""残局题库、初始局面与残局会话测试。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.puzzle_bank import load_puzzle_bank  # noqa: E402
from gomoku.client.sessions.puzzle import PuzzleSession  # noqa: E402
from gomoku.shared.game_model import GameModel  # noqa: E402
from gomoku.shared.puzzle import Puzzle, PuzzleError  # noqa: E402
from gomoku.shared.rules import Move, Player  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


class PuzzleBankTests(unittest.TestCase):
    def test_builtin_bank_has_eleven_valid_puzzles(self) -> None:
        puzzles = load_puzzle_bank()
        self.assertEqual(len(puzzles), 11)
        self.assertEqual(len({puzzle.id for puzzle in puzzles}), len(puzzles))
        self.assertTrue(all(puzzle.title for puzzle in puzzles))
        for puzzle in puzzles:
            puzzle.validate()
        self.assertEqual(
            {puzzle.title: puzzle.max_player_moves for puzzle in puzzles},
            {
                "白虹贯日": 4,
                "双管齐下": 6,
                "暗度陈仓": 5,
                "声东击西": 5,
                "黑云压城": 6,
                "白驹过隙": 5,
                "一鸣惊人": 6,
                "柳暗花明": 5,
                "峰回路转": 6,
                "白璧无瑕": 5,
                "黑马当先": 5,
            },
        )

    def test_invalid_to_move_is_rejected(self) -> None:
        with self.assertRaises(PuzzleError):
            Puzzle.from_dict(
                {
                    "id": "bad-1",
                    "title": "错误轮次",
                    "difficulty": "easy",
                    "moves": [[7, 7], [0, 0], [7, 8]],
                    "to_move": "black",
                    "max_player_moves": 1,
                }
            )

    def test_setup_with_existing_winner_is_rejected(self) -> None:
        moves = [
            [7, 4], [0, 0], [7, 5], [0, 2], [7, 6], [0, 4],
            [7, 7], [2, 0], [7, 8],
        ]
        with self.assertRaises(PuzzleError):
            Puzzle.from_dict(
                {
                    "id": "bad-2",
                    "title": "已终局",
                    "difficulty": "easy",
                    "moves": moves,
                    "to_move": "white",
                    "max_player_moves": 1,
                }
            )


class GameModelFromMovesTests(unittest.TestCase):
    def test_round_trip_rebuilds_board(self) -> None:
        moves = (
            Move(7, 7, Player.BLACK),
            Move(0, 0, Player.WHITE),
            Move(7, 8, Player.BLACK),
        )
        model = GameModel.from_moves(moves)
        self.assertEqual(model.moves, moves)
        self.assertEqual(model.board[7][7], Player.BLACK)
        self.assertEqual(model.board[7][8], Player.BLACK)
        self.assertIs(model.current_player, Player.WHITE)

    def test_wrong_turn_order_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GameModel.from_moves((Move(7, 7, Player.WHITE),))


class PuzzleSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 与题库解耦的固定测试残局：白先，一子成五，验证“玩家执先手方”。
        cls.win_puzzle = Puzzle.from_dict(
            {
                "id": "test-white-win",
                "title": "测试一子胜",
                "difficulty": "easy",
                "moves": [
                    [0, 0], [7, 4], [0, 2], [7, 5], [2, 0],
                    [7, 7], [2, 2], [7, 8], [4, 0],
                ],
                "to_move": "white",
                "max_player_moves": 1,
                "defender": "medium",
                "solution": [[7, 6]],
            }
        )

    def test_success_emits_result_and_restart_resets(self) -> None:
        session = PuzzleSession(self.win_puzzle)
        results: list[tuple[bool, str]] = []
        session.puzzle_finished.connect(
            lambda success, reason: results.append((success, reason))
        )
        try:
            self.assertTrue(session.accepts_human_input)
            self.assertTrue(session.request_move(7, 6))
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0][0])
            self.assertTrue(session.finished)
            self.assertTrue(session.succeeded)
            self.assertEqual(session.player_moves, 1)
            self.assertIs(session.view.winner, session.human_player)
            self.assertFalse(session.accepts_human_input)

            self.assertEqual(session.request_restart(), True)
            self.assertFalse(session.finished)
            self.assertEqual(session.player_moves, 0)
            self.assertEqual(session.view.move_count, self.win_puzzle.setup_move_count)
            self.assertTrue(session.accepts_human_input)
        finally:
            session.shutdown()

    def test_move_limit_failure(self) -> None:
        session = PuzzleSession(self.win_puzzle)
        results: list[tuple[bool, str]] = []
        session.puzzle_finished.connect(
            lambda success, reason: results.append((success, reason))
        )
        try:
            self.assertTrue(session.request_move(0, 5))
            self.assertEqual(len(results), 1)
            success, reason = results[0]
            self.assertFalse(success)
            self.assertTrue(session.finished)
            self.assertFalse(session.succeeded)
            self.assertIn("1", reason)
            self.assertFalse(session.request_move(0, 6))
        finally:
            session.shutdown()

    def test_undo_is_disabled(self) -> None:
        session = PuzzleSession(self.win_puzzle)
        try:
            self.assertFalse(session.can_undo)
            self.assertFalse(session.request_undo())
        finally:
            session.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
