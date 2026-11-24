"""回放审计纯函数测试：序列、结果与时间异常。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from server.app.audit import AuditMove, audit_game

_START = datetime(2026, 1, 1, 12, 0, 0)


def _winning_moves() -> list[AuditMove]:
    """黑方横向五连，白方无关落子，共 9 手。"""
    coordinates = [
        ((7, 3), "black"),
        ((0, 0), "white"),
        ((7, 4), "black"),
        ((0, 1), "white"),
        ((7, 5), "black"),
        ((0, 2), "white"),
        ((7, 6), "black"),
        ((0, 3), "white"),
        ((7, 7), "black"),
    ]
    moves: list[AuditMove] = []
    for index, ((row, col), color) in enumerate(coordinates, start=1):
        moves.append(
            AuditMove(
                move_no=index,
                user_id="black" if color == "black" else "white",
                color=color,
                row=row,
                col=col,
                created_at=_START + timedelta(seconds=index * 2),
            )
        )
    return moves


def _audit(moves, **overrides):
    params = dict(
        game_id="g1",
        black_user_id="black",
        white_user_id="white",
        status="black_won",
        winner_user_id="black",
        result_reason="five",
        move_count=len(moves),
        moves=moves,
        started_at=_START,
        ended_at=_START + timedelta(seconds=60),
    )
    params.update(overrides)
    return audit_game(**params)


class AuditSequenceTests(unittest.TestCase):
    def test_clean_winning_game_passes(self) -> None:
        result = _audit(_winning_moves())
        self.assertTrue(result.ok)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.move_count, 9)
        self.assertEqual(result.summary["moves_checked"], 9)

    def test_duplicate_coordinate_is_an_error(self) -> None:
        moves = _winning_moves()
        moves[2] = AuditMove(
            move_no=3,
            user_id="black",
            color="black",
            row=7,
            col=3,
            created_at=_START + timedelta(seconds=6),
        )
        result = _audit(moves)
        self.assertFalse(result.ok)
        self.assertIn(
            "duplicate_coordinate",
            {item.code for item in result.findings},
        )

    def test_color_order_is_an_error(self) -> None:
        moves = _winning_moves()
        moves[1] = AuditMove(
            move_no=2,
            user_id="white",
            color="black",
            row=0,
            col=0,
            created_at=_START + timedelta(seconds=4),
        )
        result = _audit(moves)
        self.assertFalse(result.ok)
        codes = {item.code for item in result.findings}
        self.assertIn("color_order", codes)
        self.assertIn("seat_mismatch", codes)

    def test_move_count_mismatch(self) -> None:
        result = _audit(_winning_moves(), move_count=12)
        self.assertFalse(result.ok)
        self.assertIn(
            "move_count_mismatch",
            {item.code for item in result.findings},
        )

    def test_time_reversal_is_an_error(self) -> None:
        moves = _winning_moves()
        moves[4] = AuditMove(
            move_no=5,
            user_id="black",
            color="black",
            row=7,
            col=5,
            created_at=_START - timedelta(seconds=10),
        )
        result = _audit(moves)
        self.assertFalse(result.ok)
        self.assertIn(
            "time_reversed",
            {item.code for item in result.findings},
        )


def _renju_overline_moves() -> list[AuditMove]:
    """黑棋第 6 颗连成 6 颗：自由规则算胜，连珠规则是长连禁手。"""
    coordinates = [
        ((7, 2), "black"),
        ((0, 0), "white"),
        ((7, 3), "black"),
        ((2, 0), "white"),
        ((7, 4), "black"),
        ((4, 0), "white"),
        ((7, 5), "black"),
        ((6, 0), "white"),
        ((7, 7), "black"),
        ((8, 0), "white"),
        ((7, 6), "black"),
    ]
    return [
        AuditMove(
            move_no=index,
            user_id=color,
            color=color,
            row=row,
            col=col,
            created_at=_START + timedelta(seconds=index * 2),
        )
        for index, ((row, col), color) in enumerate(coordinates, start=1)
    ]


class AuditRuleModeTests(unittest.TestCase):
    """审计重放必须遵循对局记录的规则模式。"""

    def test_freestyle_overline_is_still_a_win(self) -> None:
        result = _audit(_renju_overline_moves())
        self.assertTrue(result.ok, result.findings)

    def test_renju_overline_is_an_illegal_move(self) -> None:
        result = _audit(_renju_overline_moves(), rule_mode="renju")
        self.assertFalse(result.ok)
        codes = {item.code for item in result.findings}
        self.assertIn("illegal_move", codes)
        # 禁手被拒后重放棋盘没有胜者，记录的黑胜也随之对不上。
        self.assertIn("result_mismatch", codes)

    def test_renju_exact_five_is_still_a_win(self) -> None:
        result = _audit(_winning_moves(), rule_mode="renju")
        self.assertTrue(result.ok, result.findings)


class AuditResultTests(unittest.TestCase):
    def test_resign_ending_without_five_is_ok(self) -> None:
        moves = [
            AuditMove(1, "black", "black", 7, 7, _START + timedelta(seconds=2)),
            AuditMove(2, "white", "white", 7, 8, _START + timedelta(seconds=4)),
        ]
        result = _audit(
            moves,
            status="black_won",
            winner_user_id="black",
            result_reason="resign",
        )
        self.assertTrue(result.ok, result.findings)

    def test_draw_with_board_win_is_an_error(self) -> None:
        result = _audit(
            _winning_moves(),
            status="draw",
            winner_user_id=None,
            result_reason="draw",
        )
        self.assertFalse(result.ok)
        self.assertIn(
            "result_mismatch",
            {item.code for item in result.findings},
        )

    def test_five_result_without_winning_line_is_an_error(self) -> None:
        moves = _winning_moves()[:8]
        result = _audit(
            moves,
            status="black_won",
            winner_user_id="black",
            result_reason="five",
        )
        self.assertFalse(result.ok)
        self.assertIn(
            "result_mismatch",
            {item.code for item in result.findings},
        )

    def test_many_fast_responses_trigger_warning(self) -> None:
        # 双方交替、每手间隔 0.1 秒，超过阈值后应给出脚本嫌疑警告。
        moves = []
        for index in range(1, 15):
            color = "black" if index % 2 == 1 else "white"
            moves.append(
                AuditMove(
                    move_no=index,
                    user_id=color,
                    color=color,
                    row=index % 15,
                    col=index // 15,
                    created_at=_START
                    + timedelta(milliseconds=100 * index),
                )
            )
        result = _audit(
            moves,
            status="draw",
            winner_user_id=None,
            result_reason="draw",
        )
        self.assertIn(
            "suspicious_fast_moves",
            {item.code for item in result.findings},
        )
        self.assertGreaterEqual(result.fast_moves, 10)
        self.assertTrue(result.ok)  # 只是警告，不是数据错误


if __name__ == "__main__":
    unittest.main(verbosity=2)
