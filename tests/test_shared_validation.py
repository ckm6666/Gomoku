"""共享层契约测试：时限、能力、成就、残局与棋谱/协议交叉校验。

覆盖审查报告中的 G1～G6、A2～A8：这些校验都位于 ``gomoku/shared``，
客户端与服务端共用，必须有独立于 UI 的回归测试。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.shared.achievements import new_achievement_keys  # noqa: E402
from gomoku.shared.capabilities import normalize_capabilities  # noqa: E402
from gomoku.shared.errors import ProtocolError  # noqa: E402
from gomoku.shared.game_model import GameModel  # noqa: E402
from gomoku.shared.protocol import (  # noqa: E402
    CLIENT_PING,
    MAX_REQUEST_ID_LENGTH,
    PROTOCOL_VERSION,
    make_message,
    make_move_intent,
    parse_game_state,
    parse_message,
    rule_mode_from_payload,
)
from gomoku.shared.puzzle import (  # noqa: E402
    Puzzle,
    PuzzleDifficulty,
    PuzzleError,
)
from gomoku.shared.record import GameRecord, RecordError  # noqa: E402
from gomoku.shared.rules import (  # noqa: E402
    Move,
    Player,
    RuleMode,
    find_winning_line,
)
from gomoku.shared.sgf import SgfError, record_from_sgf  # noqa: E402
from gomoku.shared.time_control import (  # noqa: E402
    TimeControl,
    time_control_from_snapshot,
)


def _draw_moves() -> tuple[Move, ...]:
    """构造一盘满盘无五连的平局。

    颜色按 ``(row + 2 * col) % 4`` 的周期铺开，任意方向最多连续 2 颗；
    黑白各 113/112 子，正好可以黑白交替落满 225 手。
    """
    black: list[tuple[int, int]] = []
    white: list[tuple[int, int]] = []
    for row in range(15):
        for col in range(15):
            if (row + 2 * col) % 4 < 2:
                black.append((row, col))
            else:
                white.append((row, col))
    moves: list[Move] = []
    for index, (row, col) in enumerate(white):
        moves.append(Move(*black[index], Player.BLACK))
        moves.append(Move(row, col, Player.WHITE))
    moves.append(Move(*black[-1], Player.BLACK))
    return tuple(moves)


def _five_moves() -> tuple[Move, ...]:
    """黑棋在 row=7 连成五子、白棋陪落四手的结束局面。"""
    moves: list[Move] = []
    for index, col in enumerate(range(3, 8)):
        moves.append(Move(7, col, Player.BLACK))
        if index < 4:
            moves.append(Move(9, col, Player.WHITE))
    return tuple(moves)


def _board_values() -> list[list[str | None]]:
    return [[None for _ in range(15)] for _ in range(15)]


def _snapshot(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "game_id": "g-1",
        "board": _board_values(),
        "current_player": "black",
        "your_color": "black",
        "move_no": 0,
        "status": "playing",
    }
    payload.update(overrides)
    return payload


class GameModelContractTests(unittest.TestCase):
    """G1/G2/G3/A2：规则与模型的类型/终局契约。"""

    def test_find_winning_line_rejects_none_player(self) -> None:
        board = [[None for _ in range(15)] for _ in range(15)]
        self.assertEqual(find_winning_line(board, 7, 7, None), [])

    def test_bool_coordinates_are_rejected(self) -> None:
        model = GameModel()
        self.assertFalse(model.place_stone(True, False))
        self.assertEqual(model.move_count, 0)
        with self.assertRaises(ProtocolError):
            make_move_intent(True, False)

    def test_from_moves_raises_value_error_for_bad_coordinates(self) -> None:
        with self.assertRaises(ValueError):
            GameModel.from_moves((Move(7.5, 7, Player.BLACK),))
        with self.assertRaises(ValueError):
            GameModel.from_moves((Move(True, False, Player.BLACK),))

    def test_next_player_is_none_after_game_over(self) -> None:
        model = GameModel()
        for index, col in enumerate(range(3, 8)):
            model.place_stone(7, col)
            if index < 4:
                model.place_stone(9, col)
        self.assertIs(model.winner, Player.BLACK)
        self.assertIs(model.current_player, Player.BLACK)
        self.assertIsNone(model.next_player)


class TimeControlValidationTests(unittest.TestCase):
    """G6：工厂方法与旧字段回落使用同一套严格整数策略。"""

    def test_factories_reject_non_integers(self) -> None:
        for value in ("30", 30.9, True, None):
            with self.subTest(value=value):
                with self.assertRaises(ProtocolError):
                    TimeControl.per_move(value)  # type: ignore[arg-type]
        for value in (300.9, "300", True):
            with self.subTest(value=value):
                with self.assertRaises(ProtocolError):
                    TimeControl.fischer(value)  # type: ignore[arg-type]
        with self.assertRaises(ProtocolError):
            TimeControl.fischer(300, 3.7)  # type: ignore[arg-type]

    def test_factories_still_validate_ranges(self) -> None:
        with self.assertRaises(ProtocolError):
            TimeControl.per_move(1)
        with self.assertRaises(ProtocolError):
            TimeControl.fischer(10)
        with self.assertRaises(ProtocolError):
            TimeControl.fischer(300, 61)

    def test_valid_factories_are_unchanged(self) -> None:
        self.assertEqual(TimeControl.per_move(30).turn_seconds, 30)
        fischer = TimeControl.fischer(300, 3)
        self.assertEqual(fischer.main_seconds, 300)
        self.assertEqual(fischer.increment_seconds, 3)

    def test_legacy_fallback_rejects_invalid_values(self) -> None:
        self.assertIsNone(time_control_from_snapshot(None, None))
        self.assertEqual(
            time_control_from_snapshot(None, 45),
            TimeControl.per_move(45),
        )
        for value in ("30", 3, False, 0):
            with self.subTest(value=value):
                with self.assertRaises(ProtocolError):
                    time_control_from_snapshot(None, value)  # type: ignore[arg-type]

    def test_structured_value_takes_priority(self) -> None:
        control = time_control_from_snapshot(
            {"mode": "fischer", "main_seconds": 180, "increment_seconds": 2},
            30,
        )
        self.assertEqual(control, TimeControl.fischer(180, 2))


class CapabilityNormalizationTests(unittest.TestCase):
    """A5：未知能力被过滤，名称长度按 UTF-8 字节计算。"""

    def test_unknown_capabilities_are_filtered(self) -> None:
        self.assertEqual(
            normalize_capabilities(
                ["spectate", "unknown_feature", " undo_quota ", "spectate"]
            ),
            ("spectate", "undo_quota"),
        )
        self.assertEqual(normalize_capabilities(["future_feature"]), ())

    def test_length_is_counted_in_utf8_bytes(self) -> None:
        # 21 个汉字 = 63 字节，未超限；但它不是已知能力，安静丢弃。
        self.assertEqual(normalize_capabilities(["能" * 21]), ())
        # 22 个汉字 = 66 字节，超过 64 字节上限，必须拒绝而不是按字符放行。
        with self.assertRaises(ProtocolError):
            normalize_capabilities(["能" * 22])

    def test_malformed_values_are_still_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            normalize_capabilities("spectate")
        with self.assertRaises(ProtocolError):
            normalize_capabilities([1])
        with self.assertRaises(ProtocolError):
            normalize_capabilities(["spectate"] * 33)


class AchievementDiffTests(unittest.TestCase):
    """A6：``before`` 先归一化，脏 key 不会造成重复解锁。"""

    def test_dirty_before_key_does_not_reunlock(self) -> None:
        # 未知 key 在两侧都被过滤，不会参与“新增”判断。
        self.assertEqual(
            new_achievement_keys(("nope",), ("nope", "first_win")),
            ("first_win",),
        )
        # before 里的脏数据（非字符串、不可哈希）先归一化，不能抛 TypeError。
        self.assertEqual(
            new_achievement_keys([["first_win"], 3], ["first_win"]),
            ("first_win",),
        )

    def test_real_new_keys_still_reported(self) -> None:
        self.assertEqual(
            new_achievement_keys(
                ("rank_gold",), ("rank_gold", "first_win", "streak_3")
            ),
            ("first_win", "streak_3"),
        )


class PuzzleValidationTests(unittest.TestCase):
    """A7：坐标范围、直接构造校验与空解答线的显式标记。"""

    def test_from_dict_rejects_out_of_range_coordinates(self) -> None:
        base = {
            "id": "bad-coord",
            "title": "越界",
            "difficulty": "easy",
            "moves": [[15, 0]],
            "to_move": "white",
            "max_player_moves": 1,
        }
        with self.assertRaises(PuzzleError):
            Puzzle.from_dict(base)
        with self.assertRaises(PuzzleError):
            Puzzle.from_dict(
                dict(base, moves=[], to_move="black", solution=[[0, -1]])
            )

    def test_direct_construction_cannot_bypass_checks(self) -> None:
        common = dict(
            id="p",
            title="直接构造",
            difficulty=PuzzleDifficulty.EASY,
            moves=(),
            to_move=Player.BLACK,
            max_player_moves=1,
        )
        with self.assertRaises(PuzzleError):
            Puzzle(**dict(common, moves=(Move(15, 0, Player.BLACK),), to_move=Player.WHITE))
        with self.assertRaises(PuzzleError):
            Puzzle(**dict(common, defender="insane"))
        with self.assertRaises(PuzzleError):
            Puzzle(**dict(common, max_player_moves=0))
        with self.assertRaises(PuzzleError):
            Puzzle(**dict(common, max_player_moves=True))
        with self.assertRaises(PuzzleError):
            Puzzle(**dict(common, difficulty="easy"))  # type: ignore[arg-type]
        with self.assertRaises(PuzzleError):
            Puzzle(**dict(common, to_move="black"))  # type: ignore[arg-type]

    def test_empty_solution_is_explicit(self) -> None:
        puzzle = Puzzle(
            id="no-solution",
            title="无解答",
            difficulty=PuzzleDifficulty.EASY,
            moves=(),
            to_move=Player.BLACK,
            max_player_moves=5,
        )
        self.assertFalse(puzzle.has_solution)

        with self.assertRaises(PuzzleError):
            Puzzle(
                id="declared-empty",
                title="声明了解答线却没有",
                difficulty=PuzzleDifficulty.EASY,
                moves=(),
                to_move=Player.BLACK,
                max_player_moves=1,
                requires_solution=True,
            )
        # JSON 明确给出空数组时按数据错误处理，而不是静默放行。
        with self.assertRaises(PuzzleError):
            Puzzle.from_dict(
                {
                    "id": "bad-empty",
                    "title": "空解答",
                    "difficulty": "easy",
                    "moves": [],
                    "to_move": "black",
                    "max_player_moves": 1,
                    "solution": [],
                }
            )

    def test_valid_solution_still_accepted(self) -> None:
        puzzle = Puzzle.from_dict(
            {
                "id": "white-win",
                "title": "一子胜",
                "difficulty": "easy",
                "moves": [
                    [0, 0], [7, 4], [0, 2], [7, 5], [2, 0],
                    [7, 7], [2, 2], [7, 8], [4, 0],
                ],
                "to_move": "white",
                "max_player_moves": 1,
                "solution": [[7, 6]],
            }
        )
        self.assertTrue(puzzle.has_solution)
        self.assertEqual(len(puzzle.solution_moves), 1)


class RecordConsistencyTests(unittest.TestCase):
    """G4/A8：棋谱与最终局面必须自洽，版本号拒绝 bool。"""

    def test_full_board_draw_is_accepted(self) -> None:
        moves = _draw_moves()
        model = GameModel.from_moves(moves)
        self.assertTrue(model.is_draw)
        self.assertIsNone(model.winner)

        record = GameRecord.from_dict(
            {
                "record_id": "draw-1",
                "created_at": "2026-01-01T00:00:00+00:00",
                "mode": "local",
                "moves": [
                    {
                        "row": move.row,
                        "col": move.col,
                        "player": move.player.value,
                    }
                    for move in moves
                ],
                "winner": None,
                "result_reason": "draw",
            }
        )
        self.assertTrue(record.moves)
        self.assertEqual(record.result_reason, "draw")

    def test_contradictory_results_are_rejected(self) -> None:
        with self.assertRaises(RecordError):
            GameRecord.from_dict(
                {
                    "record_id": "bad-five",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "mode": "local",
                    "moves": [],
                    "winner": "black",
                    "result_reason": "five",
                }
            )
        with self.assertRaises(RecordError):
            GameRecord.from_dict(
                {
                    "record_id": "bad-draw",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "mode": "local",
                    "moves": [],
                    "winner": "black",
                    "result_reason": "draw",
                }
            )
        with self.assertRaises(RecordError):
            GameRecord.from_dict(
                {
                    "record_id": "bad-reason",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "mode": "local",
                    "moves": [],
                    "winner": None,
                    "result_reason": "forfeit",
                }
            )

    def test_board_win_rejects_non_five_reason(self) -> None:
        moves = [
            {
                "row": move.row,
                "col": move.col,
                "player": move.player.value,
            }
            for move in _five_moves()
        ]
        for reason in ("draw", "resign", "timeout", "escape"):
            with self.subTest(reason=reason):
                with self.assertRaises(RecordError):
                    GameRecord.from_dict(
                        {
                            "record_id": f"bad-{reason}",
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "mode": "local",
                            "moves": moves,
                            "winner": "black",
                            "result_reason": reason,
                        }
                    )
        # 留空仍兼容没有 result_reason 的旧棋谱。
        record = GameRecord.from_dict(
            {
                "record_id": "legacy-five",
                "created_at": "2026-01-01T00:00:00+00:00",
                "mode": "local",
                "moves": moves,
                "winner": "black",
                "result_reason": "",
            }
        )
        self.assertIs(record.winner, Player.BLACK)

    def test_bool_version_is_rejected(self) -> None:
        data = {
            "version": True,
            "record_id": "r-1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "mode": "local",
            "moves": [],
            "winner": None,
            "result_reason": "",
        }
        with self.assertRaises(RecordError):
            GameRecord.from_dict(data)
        data["version"] = 1
        self.assertEqual(GameRecord.from_dict(data).record_id, "r-1")


class SgfParsingTests(unittest.TestCase):
    """G4：注释里的落子不会被解析，子变化整棵跳过。"""

    def test_comment_is_not_parsed_as_moves(self) -> None:
        # 注释里的 ] 按 SGF 语法必须转义；这条注释文本包含 ";B[aa]"，
        # 旧的全文正则会把注释内容误当成落子。
        record = record_from_sgf(
            "(;GM[1]FF[4]SZ[15]C[关键点;B[aa\\];W[bb\\]];B[cc];W[dd])"
        )
        self.assertEqual(
            record.moves,
            (Move(2, 2, Player.BLACK), Move(3, 3, Player.WHITE)),
        )

        # 未转义 ] 的注释在 SGF 语法上已是畸形文本，必须明确报错，
        # 而不是把里面的 ;B[aa] 当成真实落子。
        with self.assertRaises(SgfError):
            record_from_sgf(
                "(;GM[1]FF[4]SZ[15]C[关键点;B[aa]];B[cc];W[dd])"
            )

    def test_variations_are_skipped(self) -> None:
        record = record_from_sgf(
            "(;GM[1]FF[4]SZ[15];B[aa];W[bb](;B[cc];W[dd])(;B[ee]))"
        )
        self.assertEqual(
            record.moves,
            (Move(0, 0, Player.BLACK), Move(1, 1, Player.WHITE)),
        )


class SnapshotCrossValidationTests(unittest.TestCase):
    """G5/A4：快照交叉校验与 request_id 统一限长。"""

    def test_playing_snapshot_rejects_existing_five(self) -> None:
        board = _board_values()
        for col in range(3, 8):
            board[7][col] = "black"
        with self.assertRaises(ProtocolError):
            parse_game_state(_snapshot(board=board, move_no=5))

    def test_draw_snapshot_rejects_existing_five(self) -> None:
        board = _board_values()
        for col in range(3, 8):
            board[7][col] = "black"
        with self.assertRaises(ProtocolError):
            parse_game_state(_snapshot(board=board, move_no=5, status="draw"))

    def test_move_no_must_match_stone_count(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_game_state(_snapshot(move_no=1))

    def test_bool_integer_fields_are_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_game_state(_snapshot(move_no=True))
        with self.assertRaises(ProtocolError):
            parse_game_state(_snapshot(state_version=True))
        with self.assertRaises(ProtocolError):
            parse_game_state(_snapshot(move_timeout_seconds=True))

    def test_declared_winner_must_match_board(self) -> None:
        board = _board_values()
        for col in range(3, 8):
            board[7][col] = "black"
        with self.assertRaises(ProtocolError):
            parse_game_state(
                _snapshot(board=board, move_no=5, status="white_won")
            )

    def test_snapshot_rule_mode_defaults_to_freestyle(self) -> None:
        snapshot = parse_game_state(_snapshot())
        self.assertIs(snapshot.rule_mode, RuleMode.FREESTYLE)

    def test_snapshot_rule_mode_parses_renju(self) -> None:
        snapshot = parse_game_state(_snapshot(rule_mode="renju"))
        self.assertIs(snapshot.rule_mode, RuleMode.RENJU)

    def test_snapshot_rejects_unknown_rule_mode(self) -> None:
        for value in ("chess", "", 1, False, ["renju"]):
            with self.subTest(value=value):
                with self.assertRaises(ProtocolError):
                    parse_game_state(_snapshot(rule_mode=value))

    def test_rule_mode_from_payload(self) -> None:
        self.assertIs(rule_mode_from_payload(None), RuleMode.FREESTYLE)
        self.assertIs(
            rule_mode_from_payload("freestyle"), RuleMode.FREESTYLE
        )
        self.assertIs(rule_mode_from_payload("renju"), RuleMode.RENJU)
        self.assertIs(
            rule_mode_from_payload(RuleMode.RENJU), RuleMode.RENJU
        )
        for value in ("", "Renju", 0, False):
            with self.subTest(value=value):
                with self.assertRaises(ProtocolError):
                    rule_mode_from_payload(value)

    def test_request_id_length_is_uniform(self) -> None:
        longest = "x" * MAX_REQUEST_ID_LENGTH
        message = make_message(CLIENT_PING, {}, request_id=longest)
        self.assertEqual(parse_message(message)["request_id"], longest)

        with self.assertRaises(ProtocolError):
            make_message(CLIENT_PING, {}, request_id=longest + "x")

        raw = {
            "protocol": PROTOCOL_VERSION,
            "type": CLIENT_PING,
            "payload": {},
            "request_id": longest + "x",
        }
        with self.assertRaises(ProtocolError):
            parse_message(raw)
        raw["request_id"] = 123
        with self.assertRaises(ProtocolError):
            parse_message(raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
