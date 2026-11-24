"""本地棋谱模型与 JSONL 存储测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client.history_records import record_from_game_detail  # noqa: E402
from gomoku.client.records import LocalRecordStore  # noqa: E402
from gomoku.shared.record import GameRecord, RecordError  # noqa: E402
from gomoku.shared.rules import Move, Player  # noqa: E402


def _winning_moves() -> tuple[Move, ...]:
    return (
        Move(7, 3, Player.BLACK),
        Move(9, 3, Player.WHITE),
        Move(7, 4, Player.BLACK),
        Move(9, 4, Player.WHITE),
        Move(7, 5, Player.BLACK),
        Move(9, 5, Player.WHITE),
        Move(7, 6, Player.BLACK),
        Move(9, 6, Player.WHITE),
        Move(7, 7, Player.BLACK),
    )


def make_record(
    record_id: str = "record-1",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> GameRecord:
    return GameRecord(
        record_id=record_id,
        created_at=created_at,
        mode="local",
        moves=_winning_moves(),
        winner=Player.BLACK,
        result_reason="five",
        black_name="黑棋",
        white_name="白棋",
    )


class GameRecordTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        record = make_record()
        restored = GameRecord.from_dict(record.to_dict())
        self.assertEqual(restored, record)

    def test_winner_mismatch_is_rejected(self) -> None:
        data = make_record().to_dict()
        data["winner"] = "white"
        with self.assertRaises(RecordError):
            GameRecord.from_dict(data)

    def test_resign_ending_can_have_winner_without_board_win(self) -> None:
        record = GameRecord(
            record_id="online-resign",
            created_at="2026-01-01T00:00:00+00:00",
            mode="online",
            moves=(Move(7, 7, Player.BLACK),),
            winner=Player.BLACK,
            result_reason="resign",
            black_name="甲",
            white_name="乙",
        )
        restored = GameRecord.from_dict(record.to_dict())
        self.assertIs(restored.winner, Player.BLACK)

    def test_escape_ending_can_have_winner_without_board_win(self) -> None:
        record = GameRecord(
            record_id="online-escape",
            created_at="2026-01-01T00:00:00+00:00",
            mode="online",
            moves=(Move(7, 7, Player.BLACK),),
            winner=Player.BLACK,
            result_reason="escape",
            black_name="甲",
            white_name="乙",
        )
        restored = GameRecord.from_dict(record.to_dict())
        self.assertIs(restored.winner, Player.BLACK)

    def test_empty_id_is_rejected(self) -> None:
        data = make_record().to_dict()
        data["record_id"] = ""
        with self.assertRaises(RecordError):
            GameRecord.from_dict(data)

    def test_rule_mode_defaults_to_freestyle(self) -> None:
        record = make_record()
        self.assertEqual(record.rule_mode, "freestyle")
        self.assertEqual(record.to_dict()["rule_mode"], "freestyle")
        restored = GameRecord.from_dict(record.to_dict())
        self.assertEqual(restored, record)
        self.assertEqual(restored.rule_mode, "freestyle")

    def test_renju_rule_mode_round_trip(self) -> None:
        record = GameRecord(
            record_id="renju-record",
            created_at="2026-01-01T00:00:00+00:00",
            mode="online",
            moves=_winning_moves(),
            winner=Player.BLACK,
            result_reason="five",
            rule_mode="renju",
        )
        data = record.to_dict()
        self.assertEqual(data["rule_mode"], "renju")
        restored = GameRecord.from_dict(data)
        self.assertEqual(restored, record)
        self.assertEqual(restored.rule_mode, "renju")

    def test_unknown_rule_mode_is_rejected(self) -> None:
        for value in ("chess", "", 1, None):
            with self.subTest(value=value):
                data = make_record().to_dict()
                data["rule_mode"] = value
                with self.assertRaises(RecordError):
                    GameRecord.from_dict(data)

    def test_renju_replay_rejects_black_overline(self) -> None:
        coordinates = [
            (7, 2), (0, 0),
            (7, 3), (2, 0),
            (7, 4), (4, 0),
            (7, 5), (6, 0),
            (7, 7), (8, 0),
            (7, 6),  # 第六颗黑子连成 6 颗，连珠规则下是长连禁手。
        ]
        data = {
            "record_id": "overline",
            "created_at": "2026-01-01T00:00:00+00:00",
            "mode": "local",
            "moves": [
                {
                    "row": row,
                    "col": col,
                    "player": "black" if index % 2 == 0 else "white",
                }
                for index, (row, col) in enumerate(coordinates)
            ],
            "winner": "black",
            "result_reason": "five",
            "rule_mode": "freestyle",
        }
        # 自由规则下长连算胜，棋谱合法。
        self.assertIs(
            GameRecord.from_dict(data).winner, Player.BLACK
        )
        # 连珠规则下同样的落子历史包含禁手，必须拒绝。
        data["rule_mode"] = "renju"
        with self.assertRaises(RecordError):
            GameRecord.from_dict(data)


class HistoryRecordParsingTests(unittest.TestCase):
    def test_parse_resign_detail(self) -> None:
        record = record_from_game_detail(
            {
                "game_id": "game-1",
                "rated": True,
                "status": "white_won",
                "your_color": "black",
                "black_user_id": "u1",
                "black_username": "甲",
                "white_user_id": "u2",
                "white_username": "乙",
                "winner_user_id": "u2",
                "result_reason": "resign",
                "move_no": 1,
                "created_at": "2026-01-01T00:00:00",
                "started_at": "2026-01-01T00:00:01",
                "ended_at": "2026-01-01T00:01:00",
                "rating_delta": -12,
                "moves": [
                    {
                        "move_no": 1,
                        "row": 7,
                        "col": 7,
                        "color": "black",
                        "created_at": "2026-01-01T00:00:10",
                    }
                ],
            }
        )
        self.assertEqual(record.mode, "online")
        self.assertIs(record.winner, Player.WHITE)
        self.assertEqual(record.result_reason, "resign")
        self.assertEqual(record.metadata.get("rating_delta"), -12)
        self.assertEqual(len(record.moves), 1)
        # 旧对局详情没有 rule_mode 时按自由规则处理。
        self.assertEqual(record.rule_mode, "freestyle")

    def test_parse_board_win_detail(self) -> None:
        moves = [
            {
                "move_no": index + 1,
                "row": move.row,
                "col": move.col,
                "color": move.player.value,
                "created_at": "2026-01-01T00:00:00",
            }
            for index, move in enumerate(_winning_moves())
        ]
        record = record_from_game_detail(
            {
                "game_id": "game-2",
                "rated": False,
                "status": "black_won",
                "your_color": "black",
                "black_user_id": "u1",
                "black_username": "甲",
                "white_user_id": "u2",
                "white_username": "乙",
                "winner_user_id": "u1",
                "result_reason": "five",
                "move_no": 9,
                "created_at": "2026-01-01T00:00:00",
                "moves": moves,
            }
        )
        self.assertIs(record.winner, Player.BLACK)
        self.assertEqual(len(record.moves), 9)

    def test_parse_renju_detail(self) -> None:
        """对局详情里的 rule_mode 必须带进复盘棋谱。"""
        moves = [
            {
                "move_no": index + 1,
                "row": move.row,
                "col": move.col,
                "color": move.player.value,
                "created_at": "2026-01-01T00:00:00",
            }
            for index, move in enumerate(_winning_moves())
        ]
        record = record_from_game_detail(
            {
                "game_id": "game-renju",
                "rated": False,
                "status": "black_won",
                "your_color": "black",
                "black_user_id": "u1",
                "black_username": "甲",
                "white_user_id": "u2",
                "white_username": "乙",
                "winner_user_id": "u1",
                "result_reason": "five",
                "move_no": 9,
                "created_at": "2026-01-01T00:00:00",
                "rule_mode": "renju",
                "moves": moves,
            }
        )
        self.assertEqual(record.rule_mode, "renju")
        self.assertIs(record.winner, Player.BLACK)

    def test_parse_detail_rejects_unknown_rule_mode(self) -> None:
        """未知规则模式说明数据损坏，不能悄悄按自由规则复盘。"""
        with self.assertRaises(RecordError):
            record_from_game_detail(
                {
                    "game_id": "game-bad-rule",
                    "moves": [],
                    "rule_mode": "chess",
                }
            )


class LocalRecordStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmpdir.name) / "records"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_append_list_load_and_count(self) -> None:
        store = LocalRecordStore(self.directory)
        older = make_record("older", "2026-01-01T00:00:00+00:00")
        newer = make_record("newer", "2026-01-02T00:00:00+00:00")
        store.append(older)
        store.append(newer)

        records = store.list_recent(10)
        self.assertEqual([record.record_id for record in records], ["newer", "older"])
        self.assertEqual(store.count(), 2)
        self.assertEqual(store.load("older"), older)
        self.assertIsNone(store.load("missing"))

    def test_corrupt_line_is_skipped(self) -> None:
        store = LocalRecordStore(self.directory)
        store.append(make_record())
        with store.path.open("a", encoding="utf-8") as handle:
            handle.write("{not-json}\n")
        records = store.list_recent(10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].record_id, "record-1")

    def test_prunes_to_max_records(self) -> None:
        store = LocalRecordStore(self.directory, max_records=1)
        store.append(make_record("older", "2026-01-01T00:00:00+00:00"))
        store.append(make_record("newer", "2026-01-02T00:00:00+00:00"))
        self.assertEqual(store.count(), 1)
        self.assertIsNotNone(store.load("newer"))
        self.assertIsNone(store.load("older"))

    def test_append_reports_success(self) -> None:
        """写入成功时返回 True。"""
        store = LocalRecordStore(self.directory)
        self.assertTrue(store.append(make_record()))
        self.assertEqual(store.count(), 1)

    def test_append_reports_failure(self) -> None:
        """写入失败时返回 False：调用方要能告诉用户没有保存成功。"""
        # 目录位置被一个同名文件占住，mkdir 必然失败。
        blocked = self.directory
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("not-a-directory", encoding="utf-8")
        store = LocalRecordStore(blocked)
        self.assertFalse(store.append(make_record()))
        self.assertEqual(store.count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
