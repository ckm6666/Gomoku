"""SGF 棋谱导入/导出（15 路五子棋）。

只支持单主变化（linear SGF）：导出始终生成一条主变化；导入会读取文件中
所有 ``;B[xy]`` / ``;W[xy]`` 节点，复杂分支棋谱请先整理成主变化。坐标为
SGF 标准：第一个字母是列，第二个字母是行，左上角为 ``aa``。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from gomoku.shared.game_model import GameModel
from gomoku.shared.record import GameRecord, RecordError
from gomoku.shared.rules import BOARD_SIZE, Move, Player

_SGF_LETTERS = "abcdefghijklmnopqrstuvwxyz"
_PROPERTY_RE = re.compile(r"([A-Z]{1,2})\[((?:\\.|[^\]])*)\]")
_MOVE_RE = re.compile(r";\s*([BW])\s*\[([a-z]{2})\]", re.IGNORECASE)


class SgfError(ValueError):
    """SGF 内容非法或包含不支持的结构。"""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("]", "\\]")


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _coord(row: int, col: int) -> str:
    if not 0 <= row < BOARD_SIZE or not 0 <= col < BOARD_SIZE:
        raise SgfError("落子坐标超出棋盘")
    return f"{_SGF_LETTERS[col]}{_SGF_LETTERS[row]}"


def _result_property(record: GameRecord) -> str:
    if record.winner is None:
        return "0"
    prefix = "B+" if record.winner is Player.BLACK else "W+"
    if record.result_reason in ("resign", "timeout", "escape"):
        suffix = {
            "resign": "R",
            "timeout": "T",
            "escape": "F",
        }[record.result_reason]
        return prefix + suffix
    return prefix


def record_to_sgf(record: GameRecord) -> str:
    """把一局棋谱序列化为 SGF 文本。"""
    if record.mode not in {"local", "ai", "puzzle", "online"}:
        raise SgfError("棋谱模式非法")
    date_text = record.created_at[:10]
    root = (
        "(;GM[1]FF[4]CA[UTF-8]AP[Gomoku:1.0]"
        f"SZ[{BOARD_SIZE}]"
        f"PB[{_escape(record.black_name or '黑方')}]"
        f"PW[{_escape(record.white_name or '白方')}]"
        f"DT[{_escape(date_text)}]"
        f"RE[{_result_property(record)}]"
    )
    moves = []
    for move in record.moves:
        color = "B" if move.player is Player.BLACK else "W"
        moves.append(f";{color}[{_coord(move.row, move.col)}]")
    return root + "".join(moves) + ")\n"


def _parse_date(value: str) -> str:
    text = value.strip()
    if not text:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    date_part = text[:10]
    try:
        parsed = datetime.fromisoformat(date_part)
    except ValueError:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    return parsed.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def _parse_re(value: str) -> tuple[Optional[Player], str]:
    """返回 SGF RE 中的胜者与终局后缀（R/T/F）。"""
    text = value.strip().upper()
    if not text or text in {"0", "DRAW", "D"}:
        return None, ""
    if text.startswith("B+"):
        return Player.BLACK, text[2:3]
    if text.startswith("W+"):
        return Player.WHITE, text[2:3]
    return None, ""


def record_from_sgf(text: str) -> GameRecord:
    """从单主变化 SGF 文本构造并校验 ``GameRecord``。"""
    if not isinstance(text, str) or "(" not in text or ";" not in text:
        raise SgfError("不是有效的 SGF 文本")

    properties: dict[str, str] = {}
    for match in _PROPERTY_RE.finditer(text):
        key = match.group(1).upper()
        if key in {"B", "W"}:
            continue
        properties.setdefault(key, _unescape(match.group(2)))

    size_text = properties.get("SZ", str(BOARD_SIZE))
    try:
        size = int(size_text.split(":", 1)[0])
    except ValueError as exc:
        raise SgfError("SZ 必须是整数") from exc
    if size != BOARD_SIZE:
        raise SgfError(f"只支持 {BOARD_SIZE} 路棋盘")

    moves: list[Move] = []
    for match in _MOVE_RE.finditer(text):
        color = match.group(1).upper()
        coord = match.group(2)
        col = _SGF_LETTERS.find(coord[0])
        row = _SGF_LETTERS.find(coord[1])
        if col < 0 or row < 0 or col >= BOARD_SIZE or row >= BOARD_SIZE:
            raise SgfError("落子坐标超出棋盘")
        moves.append(
            Move(
                row=row,
                col=col,
                player=Player.BLACK if color == "B" else Player.WHITE,
            )
        )

    try:
        model = GameModel.from_moves(moves)
    except ValueError as exc:
        raise SgfError(f"落子序列非法：{exc}") from exc

    re_winner, suffix = _parse_re(properties.get("RE", ""))
    if re_winner is not None and model.winner is not None:
        if re_winner is not model.winner:
            raise SgfError("RE 胜者与棋盘最终局面不一致")
    winner = re_winner or model.winner

    if winner is None:
        mode = "local"
        reason = "draw" if model.is_draw else ""
    elif model.winner is winner:
        mode = "local"
        reason = "five"
    else:
        mode = "online"
        reason = {
            "R": "resign",
            "T": "timeout",
            "F": "escape",
        }.get(suffix, "resign")

    data = {
        "record_id": uuid.uuid4().hex,
        "created_at": _parse_date(properties.get("DT", "")),
        "mode": mode,
        "moves": [
            {
                "row": move.row,
                "col": move.col,
                "player": move.player.value,
            }
            for move in moves
        ],
        "winner": winner.value if winner is not None else None,
        "result_reason": reason,
        "black_name": properties.get("PB", "黑方") or "黑方",
        "white_name": properties.get("PW", "白方") or "白方",
        "rated": False,
        "metadata": {"source": "sgf"},
    }
    try:
        return GameRecord.from_dict(data)
    except RecordError as exc:
        raise SgfError(f"棋谱校验失败：{exc}") from exc


__all__ = ["SgfError", "record_from_sgf", "record_to_sgf"]
