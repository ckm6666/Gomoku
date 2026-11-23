"""SGF 棋谱导入/导出测试。"""

from __future__ import annotations

import unittest

from gomoku.shared.record import GameRecord
from gomoku.shared.rules import Move, Player
from gomoku.shared.sgf import SgfError, record_from_sgf, record_to_sgf


def _record(
    moves: list[tuple[int, int, Player]],
    *,
    winner: Player | None,
    reason: str,
    mode: str = "local",
) -> GameRecord:
    return GameRecord(
        record_id="sgf-test",
        created_at="2026-11-23T12:00:00+00:00",
        mode=mode,
        moves=tuple(Move(row, col, player) for row, col, player in moves),
        winner=winner,
        result_reason=reason,
        black_name="甲",
        white_name="乙",
    )


class SgfRoundTripTests(unittest.TestCase):
    def test_five_in_a_row_round_trip(self) -> None:
        moves = []
        for index, col in enumerate(range(3, 8)):
            moves.append((7, col, Player.BLACK))
            if index < 4:
                moves.append((9, col, Player.WHITE))
        record = _record(moves, winner=Player.BLACK, reason="five")
        text = record_to_sgf(record)
        self.assertIn("SZ[15]", text)
        self.assertIn("B+", text)
        restored = record_from_sgf(text)
        self.assertEqual(restored.moves, record.moves)
        self.assertIs(restored.winner, Player.BLACK)
        self.assertEqual(restored.result_reason, "five")
        self.assertEqual(restored.black_name, "甲")
        self.assertEqual(restored.white_name, "乙")

    def test_resign_round_trip(self) -> None:
        record = _record(
            [(7, 7, Player.BLACK)],
            winner=Player.BLACK,
            reason="resign",
            mode="online",
        )
        restored = record_from_sgf(record_to_sgf(record))
        self.assertIs(restored.winner, Player.BLACK)
        self.assertEqual(restored.mode, "online")
        self.assertEqual(restored.result_reason, "resign")

    def test_coordinates_and_escaped_names(self) -> None:
        text = (
            "(;GM[1]FF[4]SZ[15]"
            "PB[棋手\\]甲]PW[乙\\\\二]"
            ";B[aa];W[oo])"
        )
        record = record_from_sgf(text)
        self.assertEqual(record.moves[0], Move(0, 0, Player.BLACK))
        self.assertEqual(record.moves[1], Move(14, 14, Player.WHITE))
        self.assertEqual(record.black_name, "棋手]甲")
        self.assertEqual(record.white_name, "乙\\二")

    def test_invalid_sgf_is_rejected(self) -> None:
        with self.assertRaises(SgfError):
            record_from_sgf("not sgf")
        with self.assertRaises(SgfError):
            record_from_sgf("(;GM[1]SZ[19])")
        with self.assertRaises(SgfError):
            record_from_sgf("(;GM[1]SZ[15];B[zz])")
        with self.assertRaises(SgfError):
            record_from_sgf("(;GM[1]SZ[15];B[aa];W[aa])")


if __name__ == "__main__":
    unittest.main(verbosity=2)
