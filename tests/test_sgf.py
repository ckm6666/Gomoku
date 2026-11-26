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
    rule_mode: str = "freestyle",
    metadata: dict | None = None,
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
        rule_mode=rule_mode,
        metadata=metadata or {},
    )


def _five_moves() -> list[tuple[int, int, Player]]:
    moves: list[tuple[int, int, Player]] = []
    for index, col in enumerate(range(3, 8)):
        moves.append((7, col, Player.BLACK))
        if index < 4:
            moves.append((9, col, Player.WHITE))
    return moves


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

    def test_freestyle_ruleset_is_exported_and_imported(self) -> None:
        record = _record(_five_moves(), winner=Player.BLACK, reason="five")
        text = record_to_sgf(record)
        self.assertIn("RU[freestyle]", text)
        self.assertEqual(record_from_sgf(text).rule_mode, "freestyle")

    def test_renju_ruleset_round_trip(self) -> None:
        record = _record(
            _five_moves(),
            winner=Player.BLACK,
            reason="five",
            rule_mode="renju",
        )
        text = record_to_sgf(record)
        self.assertIn("RU[renju]", text)
        restored = record_from_sgf(text)
        self.assertEqual(restored.rule_mode, "renju")
        self.assertIs(restored.winner, Player.BLACK)
        self.assertEqual(restored.result_reason, "five")

    def test_missing_ruleset_defaults_to_freestyle(self) -> None:
        restored = record_from_sgf("(;GM[1]SZ[15];B[aa];W[bb])")
        self.assertEqual(restored.rule_mode, "freestyle")

    def test_unknown_ruleset_is_rejected(self) -> None:
        for value in ("chess", "", "Renju"):
            with self.subTest(value=value):
                with self.assertRaises(SgfError):
                    record_from_sgf(
                        f"(;GM[1]SZ[15]RU[{value}];B[aa])"
                    )

    def test_invalid_record_rule_mode_is_rejected(self) -> None:
        record = _record(
            [], winner=None, reason="", rule_mode="chess"
        )
        with self.assertRaises(SgfError):
            record_to_sgf(record)

    def test_renju_import_rejects_black_overline(self) -> None:
        # 黑棋第 6 颗连成 6 颗；自由规则下算胜，连珠规则下是禁手。
        sgf = (
            "(;GM[1]SZ[15]RU[renju]"
            ";B[ch];W[aa];B[dh];W[ac];B[eh];W[ae]"
            ";B[fh];W[ag];B[hh];W[ai];B[gh])"
        )
        with self.assertRaises(SgfError):
            record_from_sgf(sgf)


class SgfCommentTests(unittest.TestCase):
    """根节点 ``C[]`` 注释（题跋）的导入导出。"""

    def test_root_comment_is_imported_as_the_colophon(self) -> None:
        record = record_from_sgf(
            "(;GM[1]SZ[15]C[第 3 手，一着之差。];B[ch];W[aa])"
        )
        self.assertEqual(record.metadata["colophon"], "第 3 手，一着之差。")

    def test_comment_on_a_move_is_not_the_colophon(self) -> None:
        """落在某一手上的 ``C[]`` 只是那一步的普通注释。

        别家 SGF 软件随手写的备注不能被当成玩家写的题跋：它会被存进
        拓片册，再导出时又被搬到根节点上。
        """
        record = record_from_sgf(
            "(;GM[1]SZ[15];B[ch];C[这是第三手的注释];W[aa])"
        )
        self.assertNotIn("colophon", record.metadata)

    def test_comment_after_the_first_move_is_not_the_colophon(self) -> None:
        record = record_from_sgf(
            "(;GM[1]SZ[15];B[ch]C[随手写的备注];W[aa])"
        )
        self.assertNotIn("colophon", record.metadata)

    def test_colophon_round_trip_through_sgf(self) -> None:
        record = _record(
            [(7, 3, Player.BLACK), (9, 3, Player.WHITE)],
            winner=None,
            reason="",
            metadata={"colophon": "第 1 手，风起。"},
        )
        text = record_to_sgf(record)
        self.assertIn("C[第 1 手，风起。]", text)
        back = record_from_sgf(text)
        self.assertEqual(back.metadata["colophon"], "第 1 手，风起。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
