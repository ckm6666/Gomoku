"""小型手工开局库。

开局库只覆盖职业/休闲对局中常见的中心区域前几手，用于在搜索前快速
给出合理应手；库外局面一律回落到 VCF/alpha-beta 搜索。加载时对每个
条目做 8 种棋盘对称展开，因此只需维护少量规范局面。

文件格式（``assets/openings/freestyle.json``）::

    {
      "version": 1,
      "ruleset": "freestyle",
      "entries": [
        {"moves": [[7, 7]], "replies": [[[6, 6], 1], [[8, 8], 1]]}
      ]
    }

``moves`` 是到达该局面前的完整落子序列，``replies`` 是应手及权重。
文件缺失或损坏时返回空库，不影响正常对局。
"""

from __future__ import annotations

import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from gomoku.client.frozen import is_frozen
from gomoku.shared.rules import BOARD_SIZE, BoardLike

logger = logging.getLogger(__name__)

Move = tuple[int, int]
_BOARD_LAST = BOARD_SIZE - 1


def _default_path() -> Path:
    """返回开局库默认路径；兼容源码与 PyInstaller。"""
    if is_frozen():
        base = Path(
            getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)
        )
        return (
            base
            / "gomoku"
            / "client"
            / "assets"
            / "openings"
            / "freestyle.json"
        )
    return (
        Path(__file__).resolve().parent
        / "assets"
        / "openings"
        / "freestyle.json"
    )


# 二面体群（旋转/镜像）的 8 种坐标变换。
_SYMMETRIES: tuple = (
    lambda row, col: (row, col),
    lambda row, col: (row, _BOARD_LAST - col),
    lambda row, col: (_BOARD_LAST - row, col),
    lambda row, col: (_BOARD_LAST - row, _BOARD_LAST - col),
    lambda row, col: (col, row),
    lambda row, col: (col, _BOARD_LAST - row),
    lambda row, col: (_BOARD_LAST - col, row),
    lambda row, col: (_BOARD_LAST - col, _BOARD_LAST - row),
)


def _parse_point(raw: Any) -> Optional[Move]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    row, col = raw
    if not isinstance(row, int) or not isinstance(col, int):
        return None
    if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
        return None
    return (row, col)


def _parse_sequence(raw: Any) -> Optional[tuple[Move, ...]]:
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    moves: list[Move] = []
    for item in raw:
        point = _parse_point(item)
        if point is None:
            return None
        moves.append(point)
    return tuple(moves)


class OpeningBook:
    """按完整落子序列索引的开局库。"""

    def __init__(
        self,
        replies: Optional[
            dict[tuple[Move, ...], dict[Move, float]]
        ] = None,
    ) -> None:
        self._replies = replies or {}

    def __len__(self) -> int:
        return len(self._replies)

    def lookup(
        self,
        history: Sequence[Move],
        board: BoardLike,
        rng: Optional[random.Random] = None,
    ) -> Optional[Move]:
        """返回库中应手；库外或全部应手非法时返回 None。"""
        key = tuple((row, col) for row, col in history)
        options = self._replies.get(key)
        if not options:
            return None

        legal = [
            (move, weight)
            for move, weight in options.items()
            if weight > 0
            and board[move[0]][move[1]] is None
            and move not in history
        ]
        if not legal:
            return None

        chooser = rng or random
        total = sum(weight for _, weight in legal)
        pick = chooser.random() * total
        cumulative = 0.0
        for move, weight in legal:
            cumulative += weight
            if pick <= cumulative:
                return move
        return legal[-1][0]


def _empty_book() -> OpeningBook:
    return OpeningBook()


def load_opening_book(
    path: Optional[Path | str] = None,
) -> OpeningBook:
    """读取并展开开局库；任何异常都返回空库。"""
    book_path = Path(path) if path is not None else _default_path()
    try:
        raw: Any = json.loads(book_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("开局库加载失败 path=%s：%s", book_path, exc)
        return _empty_book()

    if not isinstance(raw, dict):
        logger.warning("开局库根节点必须是对象")
        return _empty_book()
    if raw.get("ruleset", "freestyle") != "freestyle":
        logger.warning("开局库规则集不是 freestyle，已忽略")
        return _empty_book()
    entries = raw.get("entries")
    if not isinstance(entries, list):
        logger.warning("开局库缺少 entries 数组")
        return _empty_book()

    replies: dict[tuple[Move, ...], dict[Move, float]] = {}
    for index, entry in enumerate(entries):
        parsed = _parse_entry(entry)
        if parsed is None:
            logger.warning("跳过无效开局条目 #%s", index)
            continue
        moves, entry_replies = parsed

        for transform in _SYMMETRIES:
            transformed_moves = tuple(
                transform(row, col) for row, col in moves
            )
            bucket = replies.setdefault(transformed_moves, {})
            for reply, weight in entry_replies:
                transformed_reply = transform(*reply)
                bucket[transformed_reply] = (
                    bucket.get(transformed_reply, 0.0) + weight
                )

    return OpeningBook(replies)


def _parse_entry(
    entry: Any,
) -> Optional[tuple[tuple[Move, ...], tuple[tuple[Move, float], ...]]]:
    if not isinstance(entry, dict):
        return None
    moves = _parse_sequence(entry.get("moves"))
    if moves is None:
        return None

    raw_replies = entry.get("replies")
    if not isinstance(raw_replies, list) or not raw_replies:
        return None

    parsed_replies: list[tuple[Move, float]] = []
    for item in raw_replies:
        if isinstance(item, dict):
            reply = _parse_point(item.get("move"))
            weight = item.get("weight", 1.0)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            reply = _parse_point(item[0])
            weight = item[1]
        else:
            reply = _parse_point(item)
            weight = 1.0
        if reply is None or not isinstance(weight, (int, float)):
            continue
        if weight <= 0:
            continue
        parsed_replies.append((reply, float(weight)))

    if not parsed_replies:
        return None
    return moves, tuple(parsed_replies)


_DEFAULT_BOOK: Optional[OpeningBook] = None


def get_default_opening_book() -> OpeningBook:
    """返回进程内缓存的开局库实例。"""
    global _DEFAULT_BOOK
    if _DEFAULT_BOOK is None:
        _DEFAULT_BOOK = load_opening_book()
    return _DEFAULT_BOOK


__all__ = [
    "OpeningBook",
    "get_default_opening_book",
    "load_opening_book",
]
