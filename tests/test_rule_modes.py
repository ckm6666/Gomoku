"""规则模式（自由规则/连珠）与黑棋禁手检测的单元测试。

覆盖：默认自由规则行为不变、连珠模式下黑棋长连/四四/三三禁手、
白棋不受禁手限制、模式解析与非法历史，以及随机局面下禁手检测
不会误伤自由规则或白棋。
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.shared.game_model import GameModel  # noqa: E402
from gomoku.shared.rules import (  # noqa: E402
    BOARD_SIZE,
    DEFAULT_RULE_MODE,
    Move,
    Player,
    RuleMode,
    is_forbidden_move,
    parse_rule_mode,
)

BLACK = Player.BLACK
WHITE = Player.WHITE


def blank_board() -> list[list[Player | None]]:
    """返回 15×15 空棋盘。"""
    return [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def board_with(stones: dict[tuple[int, int], Player]) -> list[list[Player | None]]:
    """按坐标字典摆放棋子，用于直接测试无状态禁手检测。"""
    board = blank_board()
    for (row, col), player in stones.items():
        board[row][col] = player
    return board


class RuleModeTests(unittest.TestCase):
    """覆盖规则模式解析、默认行为兼容性和连珠禁手判定。"""

    def _play_alternating(
        self,
        game: GameModel,
        black_moves: list[tuple[int, int]],
        white_moves: list[tuple[int, int]],
    ) -> None:
        """按黑先白后的顺序交错落子，并断言每一手都被接受。"""
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

    def _assert_forbidden_rejected(
        self, game: GameModel, row: int, col: int
    ) -> None:
        """断言禁手落子被拒绝且对局状态完全不变。"""
        board_before = game.board
        moves_before = game.moves
        player_before = game.current_player
        count_before = game.move_count

        self.assertFalse(game.place_stone(row, col))
        self.assertEqual(game.board, board_before)
        self.assertEqual(game.moves, moves_before)
        self.assertEqual(game.current_player, player_before)
        self.assertEqual(game.move_count, count_before)
        self.assertIsNone(game.winner)
        self.assertIsNone(game.board[row][col])

    # ------------------------------------------------------------------
    # 模式解析与默认值
    # ------------------------------------------------------------------
    def test_default_mode_is_freestyle(self) -> None:
        """未显式指定模式时应保持自由规则默认值。"""
        self.assertIs(DEFAULT_RULE_MODE, RuleMode.FREESTYLE)
        self.assertIs(GameModel().rule_mode, RuleMode.FREESTYLE)
        self.assertIs(GameModel.from_moves(()).rule_mode, RuleMode.FREESTYLE)

    def test_parse_rule_mode(self) -> None:
        """字符串和枚举都可用于解析规则模式，其余输入抛 ValueError。"""
        self.assertIs(parse_rule_mode("freestyle"), RuleMode.FREESTYLE)
        self.assertIs(parse_rule_mode("renju"), RuleMode.RENJU)
        self.assertIs(parse_rule_mode(RuleMode.FREESTYLE), RuleMode.FREESTYLE)
        self.assertIs(parse_rule_mode(RuleMode.RENJU), RuleMode.RENJU)

        for bad in ("", "RENJU", "unknown", None, 1, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    parse_rule_mode(bad)

    def test_game_model_accepts_mode_values(self) -> None:
        """GameModel 构造函数与 from_moves 接受枚举或字符串，非法值报错。"""
        self.assertIs(GameModel("renju").rule_mode, RuleMode.RENJU)
        self.assertIs(GameModel(RuleMode.RENJU).rule_mode, RuleMode.RENJU)
        self.assertIs(GameModel(mode="freestyle").rule_mode, RuleMode.FREESTYLE)
        self.assertIs(
            GameModel.from_moves((), mode="renju").rule_mode, RuleMode.RENJU
        )

        with self.assertRaises(ValueError):
            GameModel("invalid")
        with self.assertRaises(ValueError):
            GameModel.from_moves((), mode="invalid")

    # ------------------------------------------------------------------
    # 自由规则保持原有行为
    # ------------------------------------------------------------------
    def test_freestyle_overline_still_wins(self) -> None:
        """默认自由规则下六连长连仍应判胜，保持既有行为不变。"""
        game = GameModel()
        black_moves = [(7, 3), (7, 4), (7, 5), (7, 7), (7, 8), (7, 6)]
        white_moves = [(0, 0), (0, 2), (0, 4), (0, 6), (0, 8)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIs(game.winner, BLACK)
        self.assertEqual(len(game.winning_line), 6)
        self.assertTrue(game.is_game_over)

    def test_freestyle_ignores_forbidden_patterns(self) -> None:
        """自由规则下四四、三三和长连都不构成禁手。"""
        double_four = board_with(
            {
                (7, 5): BLACK,
                (7, 6): BLACK,
                (7, 8): BLACK,
                (5, 7): BLACK,
                (6, 7): BLACK,
                (8, 7): BLACK,
            }
        )
        double_three = board_with(
            {(7, 6): BLACK, (7, 8): BLACK, (6, 7): BLACK, (8, 7): BLACK}
        )
        overline = board_with(
            {(7, 3): BLACK, (7, 4): BLACK, (7, 5): BLACK, (7, 7): BLACK, (7, 8): BLACK}
        )

        self.assertFalse(
            is_forbidden_move(double_four, 7, 7, BLACK, RuleMode.FREESTYLE)
        )
        self.assertFalse(
            is_forbidden_move(double_three, 7, 7, BLACK, RuleMode.FREESTYLE)
        )
        self.assertFalse(
            is_forbidden_move(overline, 7, 6, BLACK, RuleMode.FREESTYLE)
        )
        # 省略 mode 参数时同样使用自由规则默认值。
        self.assertFalse(is_forbidden_move(double_three, 7, 7, BLACK))

    # ------------------------------------------------------------------
    # 黑棋禁手
    # ------------------------------------------------------------------
    def test_renju_black_overline_rejected(self) -> None:
        """连珠规则下黑棋六连被 place_stone 拒绝，棋盘和行动方不变。"""
        game = GameModel("renju")
        black_moves = [(7, 3), (7, 4), (7, 5), (7, 7), (7, 8)]
        white_moves = [(0, 0), (0, 2), (0, 4), (0, 6), (0, 8)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertEqual(game.move_count, 10)
        self.assertIs(game.next_player, BLACK)
        self._assert_forbidden_rejected(game, 7, 6)
        self.assertIs(game.next_player, BLACK)

    def test_renju_white_overline_wins(self) -> None:
        """连珠规则下白棋长连不受限制，连续六颗直接判胜。"""
        game = GameModel("renju")
        black_moves = [(7, 7), (3, 3), (11, 11), (7, 12), (14, 0), (5, 9)]
        white_moves = [(0, 0), (0, 1), (0, 2), (0, 4), (0, 5), (0, 3)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIs(game.winner, WHITE)
        self.assertEqual(len(game.winning_line), 6)
        self.assertTrue(game.is_game_over)

    def test_renju_black_exact_five_wins(self) -> None:
        """连珠规则下黑棋恰好五连获胜。"""
        game = GameModel(RuleMode.RENJU)
        black_moves = [(7, 4), (7, 5), (7, 6), (7, 7), (7, 8)]
        white_moves = [(0, 0), (0, 2), (0, 4), (0, 6)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIs(game.winner, BLACK)
        self.assertEqual(len(game.winning_line), 5)

    def test_renju_double_four_rejected(self) -> None:
        """连珠规则下黑棋四四禁手被拒绝。"""
        game = GameModel("renju")
        black_moves = [(7, 5), (7, 6), (7, 8), (5, 7), (6, 7), (8, 7)]
        white_moves = [(0, 0), (0, 2), (0, 4), (0, 6), (0, 8), (0, 10)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIs(game.next_player, BLACK)
        self._assert_forbidden_rejected(game, 7, 7)

    def test_renju_single_four_allowed(self) -> None:
        """连珠规则下黑棋单个四（含活四）是合法落子。"""
        game = GameModel("renju")
        black_moves = [(7, 4), (7, 5), (7, 6), (7, 7)]
        white_moves = [(0, 0), (0, 2), (0, 4)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIsNone(game.winner)
        self.assertFalse(game.is_game_over)
        self.assertIs(game.next_player, WHITE)
        self.assertEqual(game.move_count, 7)

    def test_renju_double_three_rejected(self) -> None:
        """连珠规则下黑棋三三禁手被拒绝。"""
        game = GameModel("renju")
        black_moves = [(7, 6), (7, 8), (6, 7), (8, 7)]
        white_moves = [(0, 0), (0, 2), (0, 4), (0, 6)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIs(game.next_player, BLACK)
        self._assert_forbidden_rejected(game, 7, 7)

    def test_renju_single_open_three_allowed(self) -> None:
        """连珠规则下黑棋单个活三是合法落子。"""
        game = GameModel("renju")
        black_moves = [(7, 5), (7, 6), (7, 7)]
        white_moves = [(0, 0), (0, 2)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIsNone(game.winner)
        self.assertFalse(game.is_game_over)
        self.assertIs(game.next_player, WHITE)
        self.assertEqual(game.move_count, 5)

    def test_is_forbidden_move_reports_renju_patterns(self) -> None:
        """无状态检测函数应识别长连、四四、三三，并放行单四/单三。"""
        overline = board_with(
            {(7, 3): BLACK, (7, 4): BLACK, (7, 5): BLACK, (7, 7): BLACK, (7, 8): BLACK}
        )
        double_four = board_with(
            {
                (7, 5): BLACK,
                (7, 6): BLACK,
                (7, 8): BLACK,
                (5, 7): BLACK,
                (6, 7): BLACK,
                (8, 7): BLACK,
            }
        )
        double_three = board_with(
            {(7, 6): BLACK, (7, 8): BLACK, (6, 7): BLACK, (8, 7): BLACK}
        )
        single_four = board_with({(7, 4): BLACK, (7, 5): BLACK, (7, 6): BLACK})
        single_three = board_with({(7, 5): BLACK, (7, 6): BLACK})

        self.assertTrue(is_forbidden_move(overline, 7, 6, BLACK, RuleMode.RENJU))
        self.assertTrue(
            is_forbidden_move(double_four, 7, 7, BLACK, RuleMode.RENJU)
        )
        self.assertTrue(
            is_forbidden_move(double_three, 7, 7, BLACK, RuleMode.RENJU)
        )
        self.assertFalse(
            is_forbidden_move(single_four, 7, 7, BLACK, RuleMode.RENJU)
        )
        self.assertFalse(
            is_forbidden_move(single_three, 7, 7, BLACK, RuleMode.RENJU)
        )

    # ------------------------------------------------------------------
    # 白棋不受禁手限制
    # ------------------------------------------------------------------
    def test_renju_white_double_three_allowed(self) -> None:
        """连珠规则下白棋可以下三三。"""
        game = GameModel("renju")
        black_moves = [(0, 0), (0, 2), (0, 4), (0, 6), (0, 8)]
        white_moves = [(7, 6), (7, 8), (6, 7), (8, 7), (7, 7)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIsNone(game.winner)
        self.assertFalse(game.is_game_over)
        self.assertEqual(game.move_count, 10)
        self.assertIs(game.board[7][7], WHITE)

    def test_renju_white_double_four_allowed(self) -> None:
        """连珠规则下白棋可以下四四。"""
        game = GameModel("renju")
        black_moves = [
            (0, 0),
            (0, 2),
            (0, 4),
            (0, 6),
            (0, 8),
            (0, 10),
            (0, 12),
        ]
        white_moves = [(7, 5), (7, 6), (7, 8), (5, 7), (6, 7), (8, 7), (7, 7)]
        self._play_alternating(game, black_moves, white_moves)

        self.assertIsNone(game.winner)
        self.assertFalse(game.is_game_over)
        self.assertEqual(game.move_count, 14)
        self.assertIs(game.board[7][7], WHITE)

    def test_renju_white_never_forbidden(self) -> None:
        """连珠规则下白棋在任何棋形上都不会被禁手检测拦下。"""
        double_four = board_with(
            {
                (7, 5): BLACK,
                (7, 6): BLACK,
                (7, 8): BLACK,
                (5, 7): BLACK,
                (6, 7): BLACK,
                (8, 7): BLACK,
            }
        )
        double_three = board_with(
            {(7, 6): BLACK, (7, 8): BLACK, (6, 7): BLACK, (8, 7): BLACK}
        )

        self.assertFalse(
            is_forbidden_move(double_four, 7, 7, WHITE, RuleMode.RENJU)
        )
        self.assertFalse(
            is_forbidden_move(double_three, 7, 7, WHITE, RuleMode.RENJU)
        )

    # ------------------------------------------------------------------
    # 历史载入与输入防御
    # ------------------------------------------------------------------
    def test_from_moves_renju_rejects_forbidden_history(self) -> None:
        """连珠模式下带禁手的历史抛 ValueError，自由模式仍可载入。"""
        moves = [
            Move(7, 6, BLACK),
            Move(0, 0, WHITE),
            Move(7, 8, BLACK),
            Move(0, 2, WHITE),
            Move(6, 7, BLACK),
            Move(0, 4, WHITE),
            Move(8, 7, BLACK),
            Move(0, 6, WHITE),
            Move(7, 7, BLACK),
        ]

        with self.assertRaisesRegex(ValueError, "第 9 手"):
            GameModel.from_moves(moves, mode="renju")
        with self.assertRaisesRegex(ValueError, "第 9 手"):
            GameModel.from_moves(moves, mode=RuleMode.RENJU)

        freestyle = GameModel.from_moves(moves)
        self.assertIs(freestyle.rule_mode, RuleMode.FREESTYLE)
        self.assertEqual(freestyle.move_count, 9)
        self.assertIsNone(freestyle.winner)

    def test_is_forbidden_move_guards_invalid_inputs(self) -> None:
        """越界、占用、非黑棋和缺省棋盘等情况一律不判禁手。"""
        double_three = board_with(
            {(7, 6): BLACK, (7, 8): BLACK, (6, 7): BLACK, (8, 7): BLACK}
        )
        double_three[0][0] = WHITE

        for row, col in ((-1, 0), (0, -1), (15, 0), (0, 15), (True, False)):
            with self.subTest(row=row, col=col):
                self.assertFalse(
                    is_forbidden_move(double_three, row, col, BLACK, "renju")
                )

        self.assertFalse(is_forbidden_move(double_three, 0, 0, BLACK, "renju"))
        self.assertFalse(is_forbidden_move(double_three, 7, 7, None, "renju"))

    def test_random_boards_do_not_flag_freestyle_or_white(self) -> None:
        """随机棋盘上自由规则永不判禁手，连珠白棋同样永不被拦。"""
        rng = random.Random(20261125)
        cells = (None, BLACK, WHITE)

        for _ in range(25):
            board = [
                [rng.choice(cells) for _ in range(BOARD_SIZE)]
                for _ in range(BOARD_SIZE)
            ]
            for _ in range(20):
                row = rng.randrange(BOARD_SIZE)
                col = rng.randrange(BOARD_SIZE)
                self.assertFalse(
                    is_forbidden_move(board, row, col, BLACK, RuleMode.FREESTYLE)
                )
                self.assertFalse(
                    is_forbidden_move(board, row, col, WHITE, RuleMode.RENJU)
                )

    # ------------------------------------------------------------------
    # 普通开局
    # ------------------------------------------------------------------
    def test_renju_standard_opening_stays_legal(self) -> None:
        """连珠模式下中心、相邻点和常见前十手都不应被误判为禁手。"""
        game = GameModel("renju")
        opening = [
            (7, 7),
            (7, 8),
            (8, 8),
            (6, 6),
            (8, 7),
            (6, 8),
            (9, 9),
            (5, 5),
            (6, 9),
            (4, 4),
        ]

        for index, (row, col) in enumerate(opening, start=1):
            self.assertTrue(
                game.place_stone(row, col),
                msg=f"第 {index} 手普通开局不应被拒绝：{(row, col)}",
            )
        self.assertEqual(game.move_count, len(opening))
        self.assertIsNone(game.winner)
        self.assertFalse(game.is_game_over)


if __name__ == "__main__":
    unittest.main(verbosity=2)
