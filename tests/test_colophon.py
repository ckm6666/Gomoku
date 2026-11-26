"""题跋拓片：数据模型、局面编解码与本地册子存储测试。

只覆盖纯逻辑层：题跋的序列化校验、局面快照的往返、册子的读写容错，
以及导出 SGF 的往返。不依赖任何 Qt 界面。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client.colophon import (  # noqa: E402
    ALBUM_VERSION,
    MAX_COLOPHON_LENGTH,
    MAX_TITLE_LENGTH,
    POSITION_SIZE,
    VOICE_LITERAL,
    VOICE_PLAIN,
    VOICE_SELF,
    Colophon,
    ColophonError,
    ColophonStore,
    board_after,
    colophons_to_sgf,
    decode_position,
    default_directory,
    draft_colophons,
    encode_position,
    make_colophon,
    pivotal_ply,
    suggest_title,
)
from gomoku.shared.record import GameRecord  # noqa: E402
from gomoku.shared.rules import BOARD_SIZE, Move, Player  # noqa: E402
from gomoku.shared.sgf import record_from_sgf  # noqa: E402

#: 空棋盘的快照。
EMPTY_POSITION = "." * POSITION_SIZE

#: 一段黑白交替、不成五的合法落子序列。
_MOVES: tuple[Move, ...] = (
    Move(7, 7, Player.BLACK),
    Move(7, 8, Player.WHITE),
    Move(8, 7, Player.BLACK),
    Move(8, 8, Player.WHITE),
    Move(6, 6, Player.BLACK),
    Move(6, 7, Player.WHITE),
    Move(9, 9, Player.BLACK),
    Move(6, 8, Player.WHITE),
    Move(7, 6, Player.BLACK),
)


def make_record(
    record_id: str = "record-1",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> GameRecord:
    """造一份黑白交替、结果为黑胜的棋谱。"""
    return GameRecord(
        record_id=record_id,
        created_at=created_at,
        mode="local",
        moves=_MOVES,
        winner=Player.BLACK,
        result_reason="five",
        black_name="黑棋",
        white_name="白棋",
    )


def make_stored_colophon(
    colophon_id: str = "colophon-1",
    record_id: str = "record-1",
    title: str = "残阳局",
    colophon: str = "第 3 手，一着之差。",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> Colophon:
    """造一份字段齐全的拓片，用于存储层测试。"""
    return Colophon(
        id=colophon_id,
        record_id=record_id,
        title=title,
        colophon=colophon,
        ply=3,
        position=EMPTY_POSITION,
        focus_row=7,
        focus_col=7,
        focus_player="black",
        black_name="黑棋",
        white_name="白棋",
        created_at=created_at,
        verdict="失误",
        seal_glyph="闲",
        moves=((7, 7, "black"), (7, 8, "white")),
    )


def make_review(move_no: int, loss: int, label: str = "失误") -> SimpleNamespace:
    """造一个复盘结果的替身，避免依赖 review 模块的导入链。"""
    return SimpleNamespace(
        move_no=move_no,
        player=Player.BLACK,
        move=(7, 7),
        best_move=(6, 6),
        played_score=0,
        best_score=loss,
        loss=loss,
        label=label,
        win_rate_before=0.5,
        win_rate_after=0.25,
    )


class PositionCodecTests(unittest.TestCase):
    """局面快照的编码与解码。"""

    def test_encode_decode_round_trip(self) -> None:
        board = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        board[0][0] = Player.BLACK
        board[7][7] = Player.WHITE
        board[14][14] = Player.BLACK

        text = encode_position(board)
        self.assertEqual(len(text), POSITION_SIZE)
        self.assertEqual(text.count("B"), 2)
        self.assertEqual(text.count("W"), 1)
        self.assertEqual(
            decode_position(text), tuple(tuple(row) for row in board)
        )

    def test_encode_empty_board(self) -> None:
        board = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        self.assertEqual(encode_position(board), EMPTY_POSITION)

    def test_decode_rejects_wrong_length(self) -> None:
        for text in (EMPTY_POSITION[:-1], EMPTY_POSITION + "."):
            with self.subTest(length=len(text)):
                with self.assertRaises(ColophonError):
                    decode_position(text)

    def test_decode_rejects_unknown_character(self) -> None:
        text = "X" + EMPTY_POSITION[1:]
        with self.assertRaises(ColophonError):
            decode_position(text)

    def test_encode_rejects_wrong_row_count(self) -> None:
        board = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE - 1)]
        with self.assertRaises(ColophonError):
            encode_position(board)

    def test_encode_rejects_wrong_column_count(self) -> None:
        board = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        board[0] = [None] * (BOARD_SIZE - 1)
        with self.assertRaises(ColophonError):
            encode_position(board)


class BoardAfterTests(unittest.TestCase):
    """board_after 的重放语义。"""

    def test_replay_gives_expected_board(self) -> None:
        board = board_after(_MOVES, 3)
        self.assertIs(board[7][7], Player.BLACK)
        self.assertIs(board[7][8], Player.WHITE)
        self.assertIs(board[8][7], Player.BLACK)
        self.assertIsNone(board[8][8])
        self.assertIsNone(board[6][6])

    def test_ply_zero_gives_empty_board(self) -> None:
        board = board_after(_MOVES, 0)
        self.assertEqual(
            board,
            tuple(tuple([None] * BOARD_SIZE) for _ in range(BOARD_SIZE)),
        )

    def test_ply_beyond_move_count_is_rejected(self) -> None:
        with self.assertRaises(ColophonError):
            board_after(_MOVES, len(_MOVES) + 1)

    def test_negative_ply_is_rejected(self) -> None:
        with self.assertRaises(ColophonError):
            board_after(_MOVES, -1)


class ColophonSerializationTests(unittest.TestCase):
    """Colophon 的字典往返与非法数据校验。"""

    def test_round_trip(self) -> None:
        colophon = make_stored_colophon()
        restored = Colophon.from_dict(colophon.to_dict())
        self.assertEqual(restored, colophon)
        self.assertEqual(restored.moves, colophon.moves)
        self.assertEqual(restored.position, colophon.position)

    def test_from_dict_rejects_non_object(self) -> None:
        for data in ([], "题跋", 42, None):
            with self.subTest(data=data):
                with self.assertRaises(ColophonError):
                    Colophon.from_dict(data)

    def test_from_dict_rejects_bad_version(self) -> None:
        for version in (2, 0, "1", True):
            with self.subTest(version=version):
                data = make_stored_colophon().to_dict()
                data["version"] = version
                with self.assertRaises(ColophonError):
                    Colophon.from_dict(data)

    def test_from_dict_rejects_empty_title(self) -> None:
        for title in ("", "   "):
            with self.subTest(title=title):
                data = make_stored_colophon().to_dict()
                data["title"] = title
                with self.assertRaises(ColophonError):
                    Colophon.from_dict(data)

    def test_from_dict_rejects_long_title(self) -> None:
        data = make_stored_colophon().to_dict()
        data["title"] = "题" * (MAX_TITLE_LENGTH + 1)
        with self.assertRaises(ColophonError):
            Colophon.from_dict(data)

    def test_from_dict_accepts_maximum_title(self) -> None:
        data = make_stored_colophon().to_dict()
        data["title"] = "题" * MAX_TITLE_LENGTH
        restored = Colophon.from_dict(data)
        self.assertEqual(restored.title, "题" * MAX_TITLE_LENGTH)

    def test_from_dict_rejects_long_colophon(self) -> None:
        data = make_stored_colophon().to_dict()
        data["colophon"] = "文" * (MAX_COLOPHON_LENGTH + 1)
        with self.assertRaises(ColophonError):
            Colophon.from_dict(data)

    def test_from_dict_rejects_negative_ply(self) -> None:
        for ply in (-1, -10):
            with self.subTest(ply=ply):
                data = make_stored_colophon().to_dict()
                data["ply"] = ply
                with self.assertRaises(ColophonError):
                    Colophon.from_dict(data)

    def test_from_dict_rejects_malformed_position(self) -> None:
        for position in ("", EMPTY_POSITION[:-1], "X" + EMPTY_POSITION[1:]):
            with self.subTest(position=position[:8]):
                data = make_stored_colophon().to_dict()
                data["position"] = position
                with self.assertRaises(ColophonError):
                    Colophon.from_dict(data)

    def test_from_dict_rejects_non_string_position(self) -> None:
        data = make_stored_colophon().to_dict()
        data["position"] = 225
        with self.assertRaises(ColophonError):
            Colophon.from_dict(data)

    def test_from_dict_rejects_malformed_moves(self) -> None:
        bad_moves = (
            [[99, 7, "black"]],          # 行越界
            [[7, 99, "black"]],          # 列越界
            [[-1, 7, "black"]],         # 负坐标
            [[7, 7, "blue"]],            # 执子非法
            [[7, 7]],                   # 元素长度不对
            [[7, 7, "black", 1]],        # 元素长度不对
            [[7, "x", "black"]],         # 坐标不是整数
            ["7,7,black"],              # 元素不是数组
            "not-a-list",               # 整个字段不是数组
        )
        for moves in bad_moves:
            with self.subTest(moves=moves):
                data = make_stored_colophon().to_dict()
                data["moves"] = moves
                with self.assertRaises(ColophonError):
                    Colophon.from_dict(data)

    def test_from_dict_accepts_valid_moves(self) -> None:
        data = make_stored_colophon().to_dict()
        data["moves"] = [[7, 7, "black"], [7, 8, "white"]]
        restored = Colophon.from_dict(data)
        self.assertEqual(restored.moves, ((7, 7, "black"), (7, 8, "white")))

    def test_from_dict_rejects_bad_focus_player(self) -> None:
        data = make_stored_colophon().to_dict()
        data["focus_player"] = "blue"
        with self.assertRaises(ColophonError):
            Colophon.from_dict(data)

    def test_out_of_range_focus_is_normalised(self) -> None:
        """越界的点睛坐标被归一化成 -1，而不是抛错。"""
        for focus_row, focus_col in ((99, 7), (7, 99), (-1, 7), (7, -1)):
            with self.subTest(focus=(focus_row, focus_col)):
                data = make_stored_colophon().to_dict()
                data["focus_row"] = focus_row
                data["focus_col"] = focus_col
                restored = Colophon.from_dict(data)
                self.assertEqual(restored.focus_row, -1)
                self.assertEqual(restored.focus_col, -1)
                self.assertIsNone(restored.focus())


class ColophonFocusTests(unittest.TestCase):
    """点睛子与文本改写。"""

    def test_focus_returns_coordinates(self) -> None:
        self.assertEqual(make_stored_colophon().focus(), (7, 7))

    def test_focus_side_returns_player(self) -> None:
        self.assertIs(make_stored_colophon().focus_side(), Player.BLACK)

    def test_focus_side_returns_white(self) -> None:
        colophon = Colophon(
            id="c",
            record_id="r",
            title="残阳局",
            colophon="文",
            ply=1,
            position=EMPTY_POSITION,
            focus_row=0,
            focus_col=0,
            focus_player="white",
        )
        self.assertIs(colophon.focus_side(), Player.WHITE)

    def test_focus_is_none_when_unset(self) -> None:
        colophon = Colophon(
            id="c",
            record_id="r",
            title="残阳局",
            colophon="文",
            ply=0,
            position=EMPTY_POSITION,
        )
        self.assertIsNone(colophon.focus())
        self.assertIsNone(colophon.focus_side())

    def test_with_text_returns_a_copy(self) -> None:
        original = make_stored_colophon()
        updated = original.with_text(title="松风局", colophon="改写过了。")
        self.assertEqual(updated.title, "松风局")
        self.assertEqual(updated.colophon, "改写过了。")
        self.assertEqual(original.title, "残阳局")
        self.assertEqual(original.colophon, "第 3 手，一着之差。")
        self.assertEqual(updated.id, original.id)
        self.assertEqual(updated.position, original.position)

    def test_to_moves_restores_move_objects(self) -> None:
        colophon = make_stored_colophon()
        moves = colophon.to_moves()
        self.assertEqual(moves, (Move(7, 7, Player.BLACK), Move(7, 8, Player.WHITE)))
        self.assertIs(moves[0].player, Player.BLACK)
        self.assertIs(moves[1].player, Player.WHITE)

    def test_board_restores_position(self) -> None:
        colophon = make_stored_colophon()
        board = colophon.board()
        self.assertEqual(len(board), BOARD_SIZE)
        self.assertEqual(len(board[0]), BOARD_SIZE)
        self.assertIsNone(board[0][0])

    def test_to_record_keeps_colophon_in_metadata(self) -> None:
        record = make_stored_colophon().to_record()
        self.assertEqual(record.metadata["colophon"], "第 3 手，一着之差。")
        self.assertEqual(record.metadata["colophon_title"], "残阳局")
        self.assertEqual(record.mode, "local")
        self.assertEqual(record.moves[0], Move(7, 7, Player.BLACK))


class ColophonStoreTests(unittest.TestCase):
    """册子的读写与容错。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmpdir.name) / "colophons"
        self.store = ColophonStore(self.directory)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_path_and_directory(self) -> None:
        self.assertEqual(self.store.directory, self.directory)
        self.assertEqual(self.store.path, self.directory / "album.json")

    def test_add_then_list(self) -> None:
        colophon = make_stored_colophon()
        self.assertTrue(self.store.add(colophon))
        items = self.store.list_colophons()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0], colophon)

    def test_add_same_id_twice_does_not_duplicate(self) -> None:
        self.store.add(make_stored_colophon())
        self.store.add(make_stored_colophon())
        items = self.store.list_colophons()
        self.assertEqual(len(items), 1)

    def test_list_is_sorted_by_created_at_desc(self) -> None:
        self.store.add(
            make_stored_colophon("older", created_at="2026-01-01T00:00:00+00:00")
        )
        self.store.add(
            make_stored_colophon("newer", created_at="2026-01-02T00:00:00+00:00")
        )
        self.assertEqual(
            [item.id for item in self.store.list_colophons()],
            ["newer", "older"],
        )

    def test_corrupt_album_is_not_overwritten(self) -> None:
        """册子文件损坏时拒绝写入。

        把损坏当成空册子会把玩家存的每一幅拓片都覆盖掉——宁可这次
        存不进去，也不能弄丢旧的。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{ 这不是合法 JSON", encoding="utf-8")

        self.assertFalse(self.store.add(make_stored_colophon()))
        # 原始文件必须原封不动地留着。
        self.assertEqual(
            self.store.path.read_text(encoding="utf-8"),
            "{ 这不是合法 JSON",
        )

    def test_corrupt_album_is_not_emptied_by_remove(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{ 这不是合法 JSON", encoding="utf-8")

        self.assertFalse(self.store.remove("whatever"))
        self.assertEqual(
            self.store.path.read_text(encoding="utf-8"),
            "{ 这不是合法 JSON",
        )

    def test_album_with_invalid_utf8_is_not_overwritten(self) -> None:
        """册子不是合法 UTF-8 时拒绝写入，且不能让异常崩掉应用。

        ``UnicodeDecodeError`` 是 ``ValueError`` 而不是 ``OSError``，
        不接住就会一路抛到 Qt 槽里。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store.path.write_bytes(b"\xff\xfe not utf-8 \xff")

        self.assertEqual(self.store.list_colophons(), ())
        self.assertFalse(self.store.add(make_stored_colophon()))
        # 原始文件必须原封不动地留着。
        self.assertEqual(
            self.store.path.read_bytes(), b"\xff\xfe not utf-8 \xff"
        )

    def test_album_from_a_newer_version_is_not_overwritten(self) -> None:
        """版本更高的册子整册拒收。

        本程序读不懂新版册子的条目，把它当成空册子会在下一次写入时
        把它整个覆盖掉，新版存的东西就此消失。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": ALBUM_VERSION + 1,
            "colophons": [make_stored_colophon().to_dict()],
        }
        raw = json.dumps(payload, ensure_ascii=False)
        self.store.path.write_text(raw, encoding="utf-8")

        self.assertEqual(self.store.list_colophons(), ())
        self.assertFalse(self.store.add(make_stored_colophon()))
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), raw)

    def test_get_existing_and_missing(self) -> None:
        colophon = make_stored_colophon()
        self.store.add(colophon)
        self.assertEqual(self.store.get("colophon-1"), colophon)
        self.assertIsNone(self.store.get("missing"))

    def test_remove_existing_and_unknown(self) -> None:
        self.store.add(make_stored_colophon())
        self.assertTrue(self.store.remove("colophon-1"))
        self.assertEqual(self.store.list_colophons(), ())
        self.assertFalse(self.store.remove("colophon-1"))

    def test_find_by_record_filters(self) -> None:
        self.store.add(make_stored_colophon("a", record_id="record-1"))
        self.store.add(make_stored_colophon("b", record_id="record-2"))
        self.store.add(make_stored_colophon("c", record_id="record-1"))
        found = self.store.find_by_record("record-1")
        self.assertEqual(sorted(item.id for item in found), ["a", "c"])
        self.assertEqual(self.store.find_by_record("missing"), ())

    def test_missing_album_is_empty(self) -> None:
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.store.list_colophons(), ())
        self.assertIsNone(self.store.get("colophon-1"))

    def test_corrupt_album_file_is_skipped(self) -> None:
        self.store.directory.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not-json", encoding="utf-8")
        self.assertEqual(self.store.list_colophons(), ())
        self.assertIsNone(self.store.get("colophon-1"))

    def test_album_with_wrong_shape_is_skipped(self) -> None:
        self.store.directory.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text('["not", "an", "object"]', encoding="utf-8")
        self.assertEqual(self.store.list_colophons(), ())

    def test_corrupt_entry_does_not_discard_other_entries(self) -> None:
        good = make_stored_colophon("good")
        payload = {
            "version": ALBUM_VERSION,
            "colophons": [
                good.to_dict(),
                {"id": "bad", "title": "", "position": ""},
            ],
        }
        self.store.directory.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        items = self.store.list_colophons()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "good")

    def test_default_directory_is_under_app_data(self) -> None:
        self.assertEqual(default_directory().name, "colophons")


class MakeColophonTests(unittest.TestCase):
    """make_colophon 生成的拓片内容。"""

    def test_position_matches_replay(self) -> None:
        record = make_record()
        colophon = make_colophon_from_record(record, ply=3)
        self.assertEqual(
            colophon.board(), board_after(record.moves, 3)
        )
        self.assertEqual(
            colophon.position, encode_position(board_after(record.moves, 3))
        )

    def test_focus_points_at_the_ply_th_move(self) -> None:
        record = make_record()
        colophon = make_colophon_from_record(record, ply=3)
        move = record.moves[2]
        self.assertEqual(colophon.focus(), (move.row, move.col))
        self.assertEqual(colophon.focus_player, move.player.value)
        self.assertIs(colophon.focus_side(), move.player)

    def test_moves_keep_only_the_first_ply_moves(self) -> None:
        record = make_record()
        colophon = make_colophon_from_record(record, ply=4)
        self.assertEqual(len(colophon.moves), 4)
        self.assertEqual(
            colophon.moves,
            tuple(
                (move.row, move.col, move.player.value)
                for move in record.moves[:4]
            ),
        )

    def test_ply_zero_has_no_focus(self) -> None:
        colophon = make_colophon_from_record(make_record(), ply=0)
        self.assertIsNone(colophon.focus())
        self.assertIsNone(colophon.focus_side())
        self.assertEqual(colophon.moves, ())
        self.assertEqual(colophon.position, EMPTY_POSITION)

    def test_out_of_range_ply_is_rejected(self) -> None:
        record = make_record()
        for ply in (-1, len(record.moves) + 1):
            with self.subTest(ply=ply):
                with self.assertRaises(ColophonError):
                    make_colophon_from_record(record, ply=ply)

    def test_verdict_comes_from_review(self) -> None:
        record = make_record()
        reviews = (make_review(3, 12, label="失误"),)
        colophon = make_colophon_from_record(record, ply=3, reviews=reviews)
        self.assertEqual(colophon.verdict, "失误")


def make_colophon_from_record(
    record: GameRecord,
    *,
    ply: int,
    reviews: tuple = (),
) -> Colophon:
    """按默认题名与题跋生成一份拓片。"""
    return make_colophon(
        record=record,
        title=suggest_title(record.record_id),
        colophon="第 3 手，一着之差。",
        ply=ply,
        reviews=reviews,
    )


class PivotalPlyTests(unittest.TestCase):
    """关键手挑选。"""

    def test_returns_move_no_of_largest_loss(self) -> None:
        reviews = (
            make_review(1, 3),
            make_review(2, 12),
            make_review(3, 7),
        )
        self.assertEqual(pivotal_ply(reviews), 2)

    def test_empty_list_returns_none(self) -> None:
        self.assertIsNone(pivotal_ply(()))

    def test_review_without_loss_is_tolerated(self) -> None:
        review = SimpleNamespace(move_no=5)
        self.assertIsNone(pivotal_ply([review]))

    def test_a_game_without_any_loss_has_no_key_move(self) -> None:
        # 全盘都下到了最佳着法：没有"损失最大"的那一手，硬挑只会挑出
        # 随手取的第一手，题名反而指错地方。
        reviews = (
            make_review(1, 0),
            make_review(2, 0),
            make_review(3, 0),
        )
        self.assertIsNone(pivotal_ply(reviews))


class DraftColophonsTests(unittest.TestCase):
    """三种语气的题跋草稿。"""

    def test_returns_three_distinct_voices(self) -> None:
        drafts = draft_colophons(make_record(), ply=3)
        self.assertEqual(len(drafts), 3)
        voices = [voice for voice, _ in drafts]
        self.assertEqual(len(set(voices)), 3)
        self.assertEqual(
            voices, [VOICE_PLAIN, VOICE_LITERAL, VOICE_SELF]
        )

    def test_every_draft_is_non_empty_text(self) -> None:
        for voice, text in draft_colophons(make_record(), ply=3):
            with self.subTest(voice=voice):
                self.assertIsInstance(text, str)
                self.assertTrue(text.strip())

    def test_plain_voice_mentions_ply(self) -> None:
        drafts = dict(draft_colophons(make_record(), ply=3))
        self.assertIn("第 3 手", drafts[VOICE_PLAIN])
        self.assertEqual(drafts[VOICE_PLAIN], "第 3 手，黑棋胜。")

    def test_plain_voice_with_review_reports_win_rate(self) -> None:
        reviews = (make_review(3, 12, label="失误"),)
        drafts = dict(draft_colophons(make_record(), ply=3, reviews=reviews))
        self.assertEqual(drafts[VOICE_PLAIN], "第 3 手，失误，胜率 50% → 25%。")

    def test_literal_voice_uses_verdict_phrase(self) -> None:
        reviews = (make_review(3, 12, label="失误"),)
        drafts = dict(draft_colophons(make_record(), ply=3, reviews=reviews))
        self.assertEqual(drafts[VOICE_LITERAL], "第 3 手，一着之差，黑棋胜。")

    def test_self_voice_mentions_total_move_count(self) -> None:
        drafts = dict(draft_colophons(make_record(), ply=3))
        self.assertIn("第 9 手", drafts[VOICE_SELF])


class SgfExportTests(unittest.TestCase):
    """导出 SGF。"""

    def test_collection_has_one_game_per_colophon(self) -> None:
        colophons = [
            make_stored_colophon("a", colophon="第一份题跋。"),
            make_stored_colophon("b", colophon="第二份题跋。"),
        ]
        text = colophons_to_sgf(colophons)
        self.assertEqual(text.count("(;"), 2)

    def test_colophon_text_becomes_a_comment(self) -> None:
        colophon = make_stored_colophon(colophon="第 3 手，一着之差。")
        text = colophons_to_sgf([colophon])
        self.assertIn("C[第 3 手，一着之差。]", text)

    def test_empty_collection_is_empty_text(self) -> None:
        self.assertEqual(colophons_to_sgf([]), "")

    def test_round_trip_through_record_from_sgf(self) -> None:
        colophon = make_stored_colophon(colophon="第 3 手，一着之差。")
        record = record_from_sgf(colophons_to_sgf([colophon]))
        self.assertEqual(record.metadata["colophon"], colophon.colophon)
        self.assertEqual(record.moves, colophon.to_moves())

    def test_round_trip_keeps_every_colophon(self) -> None:
        colophons = [
            make_stored_colophon("a", colophon="第一份题跋。"),
            make_stored_colophon("b", colophon="第二份题跋。"),
        ]
        text = colophons_to_sgf(colophons)
        for colophon in colophons:
            with self.subTest(colophon=colophon.id):
                self.assertIn(f"C[{colophon.colophon}]", text)

    def test_export_records_the_win_when_the_position_is_a_five(self) -> None:
        """拓片正好刻在定局那一手时，导出的棋谱要写明胜负。

        否则 ``RE[0]`` 会和棋盘上已经成五的面自相矛盾：别的 SGF 软件
        会把它当成一局没下完的棋。
        """
        moves = []
        for row in range(7, 12):
            moves.append((row, 7, "black"))
            moves.append((row, 8, "white"))
        moves = moves[:-1]
        colophon = replace(
            make_stored_colophon(),
            moves=tuple(moves),
        )
        text = colophons_to_sgf([colophon])
        self.assertIn("RE[B+]", text)

    def test_export_of_an_unfinished_position_has_no_result(self) -> None:
        colophon = make_stored_colophon()
        text = colophons_to_sgf([colophon])
        self.assertIn("RE[0]", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
