"""题跋拓片：数据模型、局面编码与本地册子存储。

拓片记录的不是整局棋的终局快照，而是"决定胜负的那一手"：一份拓片
对应棋谱里的某个手数（``ply``），局面是走到那一手之后的盘面。

设计要点
--------
**拓片活得比棋谱久。** 本地棋谱上限 1000 局并按时间裁剪（见
:mod:`gomoku.client.records`），而册子是玩家的收藏。因此每份拓片都
自带一份局面快照（:func:`encode_position` 编码的 225 个字符），不依赖
对应的棋谱仍然存在——棋谱被裁掉后，拓片照样能画出来。

**纯本地。** 题跋是玩家写给自己的文字，与 AI 复盘一样不上传服务器，
兑现"复盘结果只保存在本地"的既有承诺。册子文件放在应用本地数据目录下，
不参与任何同步。

数据模型与存储都不依赖 Qt，方便在无界面的测试里直接断言。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from PySide6.QtCore import QStandardPaths

from gomoku.shared.game_model import GameModel
from gomoku.shared.record import GameRecord
from gomoku.shared.rules import BOARD_SIZE, Cell, Move, Player

logger = logging.getLogger(__name__)

#: 册子文件格式版本。
ALBUM_VERSION = 1

#: 册子文件名（位于应用本地数据目录的 ``colophons`` 子目录）。
_ALBUM_FILENAME = "album.json"

#: 题名最大字数。印文按 2×2 排布，超过 4 字排不开——引首印只刻得下
#: 前 4 个字，题名再长印面与题字就对不上了。
MAX_TITLE_LENGTH = 4

#: 题跋正文最大字数。
MAX_COLOPHON_LENGTH = 60

#: 局面快照的字符数：15×15。
POSITION_SIZE = BOARD_SIZE * BOARD_SIZE

#: 局面快照的字符约定。
_EMPTY_CELL = "."
_BLACK_CELL = "B"
_WHITE_CELL = "W"

_CELL_TO_CHAR = {
    Player.BLACK: _BLACK_CELL,
    Player.WHITE: _WHITE_CELL,
}
_CHAR_TO_CELL = {
    _BLACK_CELL: Player.BLACK,
    _WHITE_CELL: Player.WHITE,
}

#: 题名候选池：给玩家的默认题名，按棋谱 id 稳定挑选。
_TITLE_POOL = (
    "残阳局",
    "松风局",
    "寒山局",
    "听泉局",
    "孤鸿局",
    "夜航局",
    "半山局",
    "晚照局",
)

#: 题跋的三种语气。
VOICE_PLAIN = "白描"
VOICE_LITERAL = "题咏"
VOICE_SELF = "自嘲"

#: 各语气下的落子点评。
_VERDICT_PHRASE = {
    "最佳": "正着",
    "好棋": "稳",
    "一般": "平常",
    "失误": "一着之差",
    "败着": "失据",
}


class ColophonError(ValueError):
    """题跋数据非法。"""


def _parse_moves_field(
    raw: Any,
) -> tuple[tuple[int, int, str], ...]:
    """解析落子序列字段。

    每项是 ``[行, 列, 执子]``；执子取 ``"black"``/``"white"``。
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ColophonError("落子序列必须是数组")
    moves: list[tuple[int, int, str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise ColophonError("落子项必须是 [行, 列, 执子]")
        row, col, color = item
        if type(row) is not int or type(col) is not int:
            raise ColophonError("落子坐标必须是整数")
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            raise ColophonError("落子坐标超出棋盘")
        if color not in ("black", "white"):
            raise ColophonError("落子执子非法")
        moves.append((row, col, color))
    return tuple(moves)


def encode_position(board: Sequence[Sequence[Optional[Cell]]]) -> str:
    """把棋盘编码成 225 个字符的快照。

    每行 15 个字符，``.`` 表示空点，``B`` 黑子，``W`` 白子。
    """
    if len(board) != BOARD_SIZE:
        raise ColophonError(f"棋盘行数必须是 {BOARD_SIZE}")
    chars: list[str] = []
    for row in board:
        if len(row) != BOARD_SIZE:
            raise ColophonError(f"棋盘列数必须是 {BOARD_SIZE}")
        for cell in row:
            if cell is None:
                chars.append(_EMPTY_CELL)
            else:
                chars.append(_CELL_TO_CHAR[cell])
    return "".join(chars)


def decode_position(text: str) -> tuple[tuple[Optional[Cell], ...], ...]:
    """把 225 个字符的快照还原成棋盘。"""
    if len(text) != POSITION_SIZE:
        raise ColophonError(f"局面快照长度必须是 {POSITION_SIZE}")
    board: list[tuple[Optional[Cell], ...]] = []
    for row in range(BOARD_SIZE):
        cells: list[Optional[Cell]] = []
        for col in range(BOARD_SIZE):
            char = text[row * BOARD_SIZE + col]
            if char == _EMPTY_CELL:
                cells.append(None)
            elif char in _CHAR_TO_CELL:
                cells.append(_CHAR_TO_CELL[char])
            else:
                raise ColophonError(f"局面快照含未知字符：{char!r}")
        board.append(tuple(cells))
    return tuple(board)


def board_after(
    moves: Sequence[Any], ply: int
) -> tuple[tuple[Optional[Cell], ...], ...]:
    """重放前 ``ply`` 手，返回当时的局面。

    ``ply`` 是手数（1 表示走完第一手）；``ply=0`` 表示空棋盘。
    """
    if ply < 0 or ply > len(moves):
        raise ColophonError("手数超出棋谱范围")
    board: list[list[Optional[Cell]]] = [
        [None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
    ]
    for move in moves[:ply]:
        board[move.row][move.col] = move.player
    return tuple(tuple(row) for row in board)


@dataclass(frozen=True)
class Colophon:
    """一份题跋拓片。

    ``ply`` 是这张拓片所刻的手数；``position`` 是该手之后的局面快照；
    ``focus_row``/``focus_col`` 是"点睛"的那颗子（通常是关键手）。
    """

    id: str
    record_id: str
    title: str
    colophon: str
    ply: int
    position: str
    focus_row: int = -1
    focus_col: int = -1
    focus_player: str = ""
    black_name: str = ""
    white_name: str = ""
    created_at: str = ""
    verdict: str = ""
    #: 落款处的闲章字（取自 12 枚预置印章之一）；为空则不画闲章。
    seal_glyph: str = ""
    #: 走到这一手为止的落子序列（(行, 列, 执子) 三元组）。
    #: 局面快照只能还原终局，反推不出落子次序；导出 SGF 需要次序，
    #: 所以这里自带一份，册子才能脱离棋谱独立导出。
    moves: tuple[tuple[int, int, str], ...] = ()

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Colophon":
        """从字典构造并校验一份拓片。"""
        if not isinstance(data, Mapping):
            raise ColophonError("题跋必须是对象")

        version = data.get("version", ALBUM_VERSION)
        if type(version) is not int or version != ALBUM_VERSION:
            raise ColophonError(f"不支持的题跋版本：{version}")

        colophon_id = data.get("id")
        if not isinstance(colophon_id, str) or not colophon_id.strip():
            raise ColophonError("题跋 id 必须是非空字符串")

        title = data.get("title", "")
        if not isinstance(title, str):
            raise ColophonError("题名必须是字符串")
        title = title.strip()
        if not title or len(title) > MAX_TITLE_LENGTH:
            raise ColophonError(
                f"题名长度必须在 1~{MAX_TITLE_LENGTH} 之间"
            )

        text = data.get("colophon", "")
        if not isinstance(text, str):
            raise ColophonError("题跋正文必须是字符串")
        if len(text) > MAX_COLOPHON_LENGTH:
            raise ColophonError(
                f"题跋正文不能超过 {MAX_COLOPHON_LENGTH} 字"
            )

        position = data.get("position", "")
        if not isinstance(position, str):
            raise ColophonError("局面快照必须是字符串")
        # 校验字符集，长度由 decode_position 兜底。
        decode_position(position)

        ply = data.get("ply", 0)
        if type(ply) is not int or ply < 0:
            raise ColophonError("手数必须是非负整数")

        focus_row = data.get("focus_row", -1)
        focus_col = data.get("focus_col", -1)
        if type(focus_row) is not int or type(focus_col) is not int:
            raise ColophonError("点睛坐标必须是整数")
        if not (0 <= focus_row < BOARD_SIZE and 0 <= focus_col < BOARD_SIZE):
            focus_row = -1
            focus_col = -1

        focus_player = data.get("focus_player", "")
        if focus_player not in ("", "black", "white"):
            raise ColophonError("点睛棋手非法")

        created_at = data.get("created_at", "")
        if not isinstance(created_at, str):
            raise ColophonError("创建时间必须是字符串")

        seal_glyph = data.get("seal_glyph", "")
        if not isinstance(seal_glyph, str):
            raise ColophonError("闲章字必须是字符串")
        seal_glyph = seal_glyph.strip()

        moves = _parse_moves_field(data.get("moves", ()))

        return cls(
            id=colophon_id.strip(),
            record_id=str(data.get("record_id", "")),
            title=title,
            colophon=text.strip(),
            ply=ply,
            position=position,
            focus_row=focus_row,
            focus_col=focus_col,
            focus_player=focus_player,
            black_name=str(data.get("black_name", "")),
            white_name=str(data.get("white_name", "")),
            created_at=created_at,
            verdict=str(data.get("verdict", "")),
            seal_glyph=seal_glyph,
            moves=moves,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 兼容字典。"""
        return {
            "version": ALBUM_VERSION,
            "id": self.id,
            "record_id": self.record_id,
            "title": self.title,
            "colophon": self.colophon,
            "ply": self.ply,
            "position": self.position,
            "focus_row": self.focus_row,
            "focus_col": self.focus_col,
            "focus_player": self.focus_player,
            "black_name": self.black_name,
            "white_name": self.white_name,
            "created_at": self.created_at,
            "verdict": self.verdict,
            "seal_glyph": self.seal_glyph,
            "moves": [list(move) for move in self.moves],
        }

    # ------------------------------------------------------------------
    # 派生
    # ------------------------------------------------------------------
    def board(self) -> tuple[tuple[Optional[Cell], ...], ...]:
        """还原这张拓片对应的局面。"""
        return decode_position(self.position)

    def focus(self) -> Optional[tuple[int, int]]:
        """返回点睛子坐标；没有点睛子时返回 ``None``。"""
        if self.focus_row < 0 or self.focus_col < 0:
            return None
        return (self.focus_row, self.focus_col)

    def focus_side(self) -> Optional[Player]:
        """返回点睛子的执子方。"""
        if self.focus_player == "black":
            return Player.BLACK
        if self.focus_player == "white":
            return Player.WHITE
        return None

    def with_text(self, *, title: str, colophon: str) -> "Colophon":
        """返回改写了题名与题跋的副本。"""
        return replace(self, title=title, colophon=colophon)

    def to_moves(self) -> tuple[Any, ...]:
        """还原成 :class:`gomoku.shared.rules.Move` 序列。"""
        return tuple(
            Move(
                row,
                col,
                Player.BLACK if color == "black" else Player.WHITE,
            )
            for row, col, color in self.moves
        )

    def to_record(self) -> GameRecord:
        """还原成一份可导出 SGF 的棋谱。

        胜负从落子序列里重放得出：拓片常常正好刻在定局的那一手，
        棋盘上已经成五却写 ``RE[0]`` 会让导出的棋谱自相矛盾——
        别的 SGF 软件会把它当成一局没下完的棋。
        """
        moves = self.to_moves()
        winner: Optional[Player] = None
        reason = ""
        try:
            model = GameModel.from_moves(moves, mode="freestyle")
        except ValueError:
            # 落子序列不合法（册子被外部改坏过）：不猜胜负，按快照导出。
            logger.warning("拓片落子序列非法，导出不带胜负")
        else:
            winner = model.winner
            if winner is not None:
                reason = "five"
        return GameRecord(
            record_id=self.record_id or self.id,
            created_at=self.created_at,
            mode="local",
            moves=moves,
            winner=winner,
            result_reason=reason,
            black_name=self.black_name,
            white_name=self.white_name,
            rated=False,
            metadata={"colophon": self.colophon, "colophon_title": self.title},
        )


def suggest_title(record_id: str) -> str:
    """按棋谱 id 稳定地挑一个默认题名。"""
    if not record_id:
        return _TITLE_POOL[0]
    index = sum(ord(char) for char in record_id) % len(_TITLE_POOL)
    return _TITLE_POOL[index]


def pivotal_ply(reviews: Sequence[Any]) -> Optional[int]:
    """从 AI 复盘结果里找出"关键手"的手数。

    关键手是评分损失最大的那一手——通常是决定胜负的一步错棋。
    一手都没损失时（全盘都下到了最佳着法）没有关键手，返回 ``None``：
    此时硬挑一手只会挑出随手取的第一手，题名反而指错了地方。
    """
    best_no: Optional[int] = None
    best_loss: float = 0
    for review in reviews:
        loss = getattr(review, "loss", 0)
        move_no = getattr(review, "move_no", 0)
        if isinstance(loss, bool) or not isinstance(loss, (int, float)):
            continue
        if loss > best_loss:
            best_loss = loss
            best_no = move_no
    return best_no


def _outcome_phrase(record: GameRecord) -> str:
    """一句话概括棋局结果。"""
    if record.winner is None:
        if record.result_reason == "draw":
            return "和棋"
        return "未分胜负"
    name = (
        record.black_name
        if record.winner is Player.BLACK
        else record.white_name
    )
    return f"{name or ('黑棋' if record.winner is Player.BLACK else '白棋')}胜"


def draft_colophons(
    record: GameRecord,
    *,
    ply: int,
    reviews: Sequence[Any] = (),
) -> tuple[tuple[str, str], ...]:
    """为一份拓片起草三种语气的题跋。

    返回 ``((语气, 文本), ...)``。题跋是玩家写给自己的文字，起草只是
    给个开头，玩家可以随意改写——所以这里不做花哨的修辞，只把引擎
    算出来的数字如实摆出来。
    """
    by_move = {
        getattr(review, "move_no", 0): review for review in reviews
    }
    review = by_move.get(ply)
    label = getattr(review, "label", "") if review else ""
    phrase = _VERDICT_PHRASE.get(label, "")
    outcome = _outcome_phrase(record)

    if review is not None:
        before = getattr(review, "win_rate_before", 0.5)
        after = getattr(review, "win_rate_after", 0.5)
        plain = (
            f"第 {ply} 手，{label}，"
            f"胜率 {round(before * 100)}% → {round(after * 100)}%。"
        )
    else:
        plain = f"第 {ply} 手，{outcome}。"

    if phrase:
        literal = f"第 {ply} 手，{phrase}，{outcome}。"
    else:
        literal = f"{outcome}。第 {ply} 手，回看几番。"

    total = len(record.moves)
    if total > ply:
        self_voice = (
            f"第 {ply} 手已知不可为而为之，第 {total} 手方知悔。"
        )
    else:
        self_voice = f"第 {ply} 手，一步之差，{outcome}。"

    return (
        (VOICE_PLAIN, plain),
        (VOICE_LITERAL, literal),
        (VOICE_SELF, self_voice),
    )


def _online_outcome_phrase(
    record: GameRecord, your_color: Optional[Player]
) -> str:
    """联机对局的结局措辞。"""
    if record.winner is None:
        return "和棋"
    if your_color is None:
        # 不知道自己是哪一方时，只说胜负本身。
        name = (
            record.black_name
            if record.winner is Player.BLACK
            else record.white_name
        )
        return f"{name or '对手'}胜"
    if record.winner is your_color:
        return "胜"
    return "惜败"


def draft_online_colophons(
    record: GameRecord,
    *,
    your_color: Optional[Player] = None,
    finished_by_five: bool = False,
) -> tuple[tuple[str, str], ...]:
    """为联机对局起草题跋。

    联机对局在本地没有 AI 复盘，拿不到引擎的逐手评定，因此这里只依据
    胜负、终局方式与手数来写。写得保守，但至少是一句完整的判断——玩家
    随时可以改写。
    """
    total = len(record.moves)
    if total <= 0:
        return (
            (VOICE_PLAIN, "此局未落子。"),
            (VOICE_LITERAL, "空局一场。"),
            (VOICE_SELF, "一子未落，各自收手。"),
        )

    outcome = _online_outcome_phrase(record, your_color)
    phrase = "五连定局" if finished_by_five else "分出胜负"

    plain = f"第 {total} 手，{outcome}。"
    literal = f"第 {total} 手，{phrase}，{outcome}。"

    if your_color is not None and record.winner is not None:
        if record.winner is your_color:
            self_voice = f"第 {total} 手落定，{outcome}，侥幸。"
        else:
            self_voice = f"第 {total} 手，才知不可为而为之。"
    else:
        self_voice = f"第 {total} 手，{outcome}。"

    return (
        (VOICE_PLAIN, plain),
        (VOICE_LITERAL, literal),
        (VOICE_SELF, self_voice),
    )


def make_colophon(
    record: GameRecord,
    *,
    title: str,
    colophon: str,
    ply: int,
    reviews: Sequence[Any] = (),
) -> Colophon:
    """根据棋谱与手数生成一份拓片。

    局面由前 ``ply`` 手重放得出；点睛子取第 ``ply`` 手。
    """
    if ply < 0 or ply > len(record.moves):
        raise ColophonError("手数超出棋谱范围")

    board = board_after(record.moves, ply)
    focus_row = -1
    focus_col = -1
    focus_player = ""
    verdict = ""
    if ply >= 1:
        move = record.moves[ply - 1]
        focus_row = move.row
        focus_col = move.col
        focus_player = move.player.value
        for review in reviews:
            if getattr(review, "move_no", 0) == ply:
                verdict = getattr(review, "label", "")
                break

    return Colophon(
        id=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
        record_id=record.record_id,
        title=title.strip(),
        colophon=colophon.strip(),
        ply=ply,
        position=encode_position(board),
        focus_row=focus_row,
        focus_col=focus_col,
        focus_player=focus_player,
        black_name=record.black_name,
        white_name=record.white_name,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        verdict=verdict,
        moves=tuple(
            (move.row, move.col, move.player.value)
            for move in record.moves[:ply]
        ),
    )


class ColophonStore:
    """题跋册子的本地存储。

    整个册子是一个 JSON 文件：``{"version": 1, "colophons": [...]}``。
    写入走临时文件 + 原子替换，避免中途崩溃留下半个文件；读取时跳过
    损坏条目，尽量不因为一条坏数据就丢掉整册。
    """

    def __init__(self, directory: Optional[Path] = None) -> None:
        self._directory = (
            Path(directory) if directory is not None else default_directory()
        )

    @property
    def directory(self) -> Path:
        """册子所在目录。"""
        return self._directory

    @property
    def path(self) -> Path:
        """册子文件路径。"""
        return self._directory / _ALBUM_FILENAME

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def list_colophons(self) -> tuple[Colophon, ...]:
        """按创建时间倒序返回全部拓片。"""
        items = self._read_all()
        return tuple(
            sorted(
                items,
                key=lambda item: item.created_at,
                reverse=True,
            )
        )

    def read_all(self) -> Optional[tuple[Colophon, ...]]:
        """读取全部拓片；册子读不了时返回 ``None``。

        与 :meth:`list_colophons` 的区别在于"空册子"和"册子坏了"是两
        回事：前者是玩家还没存过拓片，后者是文件损坏或版本对不上。
        界面要区分这两种情况——把坏掉的册子显示成"还没有拓片"，玩家
        会以为自己的收藏凭空没了。
        """
        return self._load()

    def get(self, colophon_id: str) -> Optional[Colophon]:
        """按 id 查找一份拓片。"""
        for item in self._read_all():
            if item.id == colophon_id:
                return item
        return None

    def find_by_record(self, record_id: str) -> tuple[Colophon, ...]:
        """找出与某份棋谱关联的全部拓片。"""
        return tuple(
            item
            for item in self._read_all()
            if item.record_id == record_id
        )

    def _read_all(self) -> tuple[Colophon, ...]:
        """读取全部拓片；册子损坏时返回空元组。"""
        loaded = self._load()
        return () if loaded is None else loaded

    def _load(self) -> Optional[tuple[Colophon, ...]]:
        """读取册子；文件损坏时返回 ``None`` 以便与"空册子"区分开。

        区分这两种情况是必要的：写入时如果分不清，损坏的册子会被当成
        空册子覆盖，玩家存的每一幅拓片就此消失。
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except UnicodeDecodeError:
            # 不是 UTF-8：多半是别处的编辑器改写了册子，或上一次写入
            # 留下了半个多字节字符。它是 ValueError 而不是 OSError，
            # 不接住就会一路抛到 Qt 槽里，把整个应用带崩。
            logger.error("题跋册子不是合法 UTF-8 path=%s", self.path)
            return None
        except OSError:
            logger.exception("读取题跋册子失败 path=%s", self.path)
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.error("题跋册子不是合法 JSON path=%s", self.path)
            return None
        if not isinstance(data, dict):
            logger.error("题跋册子格式非法 path=%s", self.path)
            return None
        # 册子版本必须对得上：版本更高的册子是新版程序写的，本程序
        # 读不懂它的条目，只能整册拒收。把它当成空册子会立刻被下一次
        # 写入覆盖，新版存的东西就此消失。
        version = data.get("version", ALBUM_VERSION)
        if type(version) is not int or version != ALBUM_VERSION:
            logger.error(
                "题跋册子版本不支持：%s path=%s", version, self.path
            )
            return None
        items = data.get("colophons")
        if not isinstance(items, list):
            logger.error("题跋册子缺少 colophons 数组 path=%s", self.path)
            return None
        result: list[Colophon] = []
        for entry in items:
            try:
                result.append(Colophon.from_dict(entry))
            except ColophonError:
                logger.warning("跳过一条损坏的题跋记录")
                continue
        return tuple(result)

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def add(self, colophon: Colophon) -> bool:
        """把一份拓片写入册子；返回是否成功。

        册子文件已经损坏时拒绝写入：把损坏当成空册子会把玩家存的
        拓片全部覆盖掉，宁可这次存不进去，也不能弄丢旧的。
        """
        loaded = self._load()
        if loaded is None:
            logger.error("题跋册子已损坏，拒绝写入 path=%s", self.path)
            return False
        items = [item for item in loaded if item.id != colophon.id]
        items.append(colophon)
        return self._write_all(items)

    def remove(self, colophon_id: str) -> bool:
        """从册子里删掉一份拓片；返回是否找到并删除。"""
        loaded = self._load()
        if loaded is None:
            logger.error("题跋册子已损坏，拒绝修改 path=%s", self.path)
            return False
        items = list(loaded)
        remaining = [item for item in items if item.id != colophon_id]
        if len(remaining) == len(items):
            return False
        return self._write_all(remaining)

    def _write_all(self, items: Sequence[Colophon]) -> bool:
        payload = {
            "version": ALBUM_VERSION,
            "colophons": [item.to_dict() for item in items],
        }
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._directory,
                prefix=f"{_ALBUM_FILENAME}.",
                suffix=".tmp",
                delete=False,
            )
            try:
                with handle:
                    json.dump(
                        payload,
                        handle,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                os.replace(handle.name, self.path)
            except BaseException:
                # 替换失败时别把临时文件留在目录里。
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass
                raise
        except OSError:
            logger.exception("写入题跋册子失败 path=%s", self.path)
            return False
        return True


def colophons_to_sgf(colophons: Sequence[Colophon]) -> str:
    """把整册拓片导出成一个 SGF 集文本。

    每份拓片对应 SGF 集里的一局，题跋写在根节点的 ``C[]`` 注释上。
    """
    from gomoku.shared.sgf import record_to_sgf, sgf_collection

    games = [record_to_sgf(colophon.to_record()) for colophon in colophons]
    return sgf_collection(games)


def default_directory() -> Path:
    """返回应用本地数据目录下的 ``colophons`` 目录。"""
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    if not base:
        base = str(Path.home() / ".gomoku" / "Gomoku")
    return Path(base) / "colophons"


__all__ = [
    "ALBUM_VERSION",
    "MAX_COLOPHON_LENGTH",
    "MAX_TITLE_LENGTH",
    "POSITION_SIZE",
    "VOICE_LITERAL",
    "VOICE_PLAIN",
    "VOICE_SELF",
    "Colophon",
    "ColophonError",
    "ColophonStore",
    "board_after",
    "colophons_to_sgf",
    "decode_position",
    "default_directory",
    "draft_colophons",
    "draft_online_colophons",
    "encode_position",
    "make_colophon",
    "pivotal_ply",
    "suggest_title",
]
