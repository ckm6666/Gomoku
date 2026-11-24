"""G19：共享规则与各调用端的随机等价性属性测试。

审查结论（issue #33 G19）指出坐标校验与胜负判定在多处重复实现，容易
随规则演进漂移。这里用随机局面把“唯一实现”与各调用路径钉在一起：

* ``in_bounds`` 是唯一的边界判定；
* ``would_win`` 是唯一的“落子即胜”判定，等价于虚拟落子后调用
  ``find_winning_line``；
* ``GameModel.is_legal_move`` 必须与 ``place_stone`` 接受与否完全一致
  （包括连珠模式下黑棋的禁手点）；
* 调用端（AI 搜索）复用同一判定；SGF / 开局库的坐标解析复用同一套
  边界规则（布尔值不再被当成坐标）。
"""

from __future__ import annotations

import random
import unittest

from gomoku.client import opening_book
from gomoku.shared import sgf
from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import (
    BOARD_SIZE,
    Move,
    Player,
    RuleMode,
    find_winning_line,
    in_bounds,
    is_forbidden_move,
    would_win,
)

_RANDOM_SEED = 20261125


def _random_board(rng: random.Random) -> list[list[Player | None]]:
    """生成一张随机棋盘；允许已经出现五连，属性测试不依赖局面合法性。"""
    board: list[list[Player | None]] = [
        [None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
    ]
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            value = rng.random()
            if value < 0.22:
                board[row][col] = Player.BLACK
            elif value < 0.44:
                board[row][col] = Player.WHITE
    return board


def _empty_cells(board: list[list[Player | None]]) -> list[tuple[int, int]]:
    return [
        (row, col)
        for row in range(BOARD_SIZE)
        for col in range(BOARD_SIZE)
        if board[row][col] is None
    ]


def _random_legal_moves(
    rng: random.Random, mode: RuleMode
) -> list[Move]:
    """随机走若干合法手，返回落子历史。"""
    model = GameModel(mode)
    moves: list[Move] = []
    for _ in range(rng.randint(1, 24)):
        if model.is_game_over:
            break
        candidates = [
            (row, col)
            for row in range(BOARD_SIZE)
            for col in range(BOARD_SIZE)
            if model.is_legal_move(row, col)
        ]
        if not candidates:
            break
        row, col = rng.choice(candidates)
        player = model.current_player
        if not model.place_stone(row, col):
            break
        moves.append(Move(row, col, player))
    return moves


class CoordinateBoundsTests(unittest.TestCase):
    """边界判定只有 ``in_bounds`` 一个口径。"""

    def test_in_bounds_rejects_non_int_and_out_of_range(self) -> None:
        for row, col in (
            (-1, 0),
            (0, -1),
            (BOARD_SIZE, 0),
            (0, BOARD_SIZE),
            (True, 0),
            (0, False),
            (1.0, 0),
            ("1", 0),
        ):
            with self.subTest(row=row, col=col):
                self.assertFalse(in_bounds(row, col))  # type: ignore[arg-type]

    def test_in_bounds_accepts_every_board_point(self) -> None:
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                self.assertTrue(in_bounds(row, col))

    def test_sgf_and_opening_book_share_the_same_bounds_rule(self) -> None:
        # 布尔值以前会因为 isinstance(True, int) 被当成坐标；统一走
        # in_bounds 后必须与规则层一样拒绝。
        self.assertIsNone(opening_book._parse_point((True, 0)))
        self.assertIsNone(opening_book._parse_point((0, BOARD_SIZE)))
        with self.assertRaises(sgf.SgfError):
            sgf._coord(True, 0)


class WouldWinEquivalenceTests(unittest.TestCase):
    """``would_win`` 与“虚拟落子后查五连”必须逐点等价。"""

    def test_random_boards_match_find_winning_line(self) -> None:
        rng = random.Random(_RANDOM_SEED)
        checked = 0
        for _ in range(120):
            board = _random_board(rng)
            empty = _empty_cells(board)
            if not empty:
                continue
            for _ in range(4):
                row, col = rng.choice(empty)
                player = rng.choice((Player.BLACK, Player.WHITE))
                probe = [list(board_row) for board_row in board]
                probe[row][col] = player
                expected = bool(
                    find_winning_line(probe, row, col, player)
                )
                actual = would_win(board, row, col, player)
                self.assertEqual(
                    actual, expected, f"({row}, {col}) {player}"
                )
                checked += 1
        self.assertGreater(checked, 200)

    def test_would_win_rejects_bad_inputs(self) -> None:
        board = [
            [None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
        ]
        self.assertFalse(would_win(board, 0, 0, None))
        self.assertFalse(would_win(board, -1, 0, Player.BLACK))
        self.assertFalse(would_win(board, 0, BOARD_SIZE, Player.BLACK))
        board[7][7] = Player.BLACK
        self.assertFalse(would_win(board, 7, 7, Player.BLACK))

    def test_game_model_win_matches_would_win(self) -> None:
        rng = random.Random(_RANDOM_SEED + 1)
        for mode in (RuleMode.FREESTYLE, RuleMode.RENJU):
            for _ in range(30):
                moves = _random_legal_moves(rng, mode)
                model = GameModel.from_moves(moves, mode=mode)
                if model.is_game_over:
                    continue
                empty = _empty_cells(list(map(list, model.board)))
                for row, col in empty[:8]:
                    if not model.is_legal_move(row, col):
                        continue
                    clone = GameModel.from_moves(moves, mode=mode)
                    predicted = would_win(
                        clone.board, row, col, clone.current_player
                    )
                    self.assertTrue(clone.place_stone(row, col))
                    self.assertEqual(
                        clone.is_game_over and clone.winner is not None,
                        predicted,
                        f"{mode.value} ({row}, {col})",
                    )


class LegalMoveMatchesPlaceStoneTests(unittest.TestCase):
    """``is_legal_move`` 必须与 ``place_stone`` 的接受与否一致。"""

    def test_random_freestyle_positions(self) -> None:
        self._assert_consistency(RuleMode.FREESTYLE)

    def test_random_renju_positions(self) -> None:
        self._assert_consistency(RuleMode.RENJU)

    def _assert_consistency(self, mode: RuleMode) -> None:
        rng = random.Random(_RANDOM_SEED + 2)
        for _ in range(4):
            moves = _random_legal_moves(rng, mode)
            model = GameModel.from_moves(moves, mode=mode)
            for row in range(BOARD_SIZE):
                for col in range(BOARD_SIZE):
                    expected = model.is_legal_move(row, col)
                    clone = GameModel.from_moves(moves, mode=mode)
                    actual = clone.place_stone(row, col)
                    self.assertEqual(
                        actual,
                        expected,
                        f"{mode.value} ({row}, {col})",
                    )
                    if expected:
                        self.assertIsNotNone(clone.board[row][col])

    def test_renju_forbidden_point_is_illegal(self) -> None:
        # 黑棋在 (7,4..6) 与 (4..6,7) 各有一组三，落 (7,7) 同时造出
        # 横向与纵向两个四，构成四四禁手；白棋在远端交替落子补足手数。
        moves = [
            Move(7, 4, Player.BLACK),
            Move(0, 0, Player.WHITE),
            Move(7, 5, Player.BLACK),
            Move(0, 2, Player.WHITE),
            Move(7, 6, Player.BLACK),
            Move(0, 4, Player.WHITE),
            Move(4, 7, Player.BLACK),
            Move(0, 6, Player.WHITE),
            Move(5, 7, Player.BLACK),
            Move(0, 8, Player.WHITE),
            Move(6, 7, Player.BLACK),
            Move(0, 10, Player.WHITE),
        ]
        model = GameModel.from_moves(moves, mode=RuleMode.RENJU)
        self.assertIs(model.current_player, Player.BLACK)
        self.assertFalse(model.is_game_over)
        self.assertTrue(
            is_forbidden_move(
                model.board, 7, 7, Player.BLACK, RuleMode.RENJU
            )
        )
        self.assertFalse(model.is_legal_move(7, 7))
        self.assertFalse(
            GameModel.from_moves(moves, mode=RuleMode.RENJU).place_stone(7, 7)
        )
        # 自由规则下同一手完全合法（且会形成两个四，但不算五连）。
        freestyle = GameModel.from_moves(moves, mode=RuleMode.FREESTYLE)
        self.assertTrue(freestyle.is_legal_move(7, 7))
        self.assertTrue(freestyle.place_stone(7, 7))


class CrossEndEquivalenceTests(unittest.TestCase):
    """AI 搜索用的落子即胜助手必须与共享规则逐点一致。"""

    def test_ai_search_context_matches_shared_rule(self) -> None:
        from gomoku.client.ai_engine import (
            DIFFICULTY_CONFIGS,
            _SearchContext,
        )
        from gomoku.client.ai_engine import AIDifficulty

        rng = random.Random(_RANDOM_SEED + 3)
        for _ in range(12):
            board = _random_board(rng)
            context = _SearchContext(
                board,
                Player.BLACK,
                AIDifficulty.EASY,
                DIFFICULTY_CONFIGS[AIDifficulty.EASY],
                None,
                random.Random(0),
            )
            empty = _empty_cells(board)
            if not empty:
                continue
            for _ in range(6):
                row, col = rng.choice(empty)
                player = rng.choice((Player.BLACK, Player.WHITE))
                self.assertEqual(
                    context._would_win_at(row, col, player),
                    would_win(board, row, col, player),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
