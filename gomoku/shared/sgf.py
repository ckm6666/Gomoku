"""SGF 棋谱导入/导出（15 路五子棋）。

只支持单主变化（linear SGF）：导出始终生成一条主变化；导入按 SGF 的节点
边界解析，只读取主变化上的 ``;B[xy]`` / ``;W[xy]`` 节点——注释
（``C[...]``）里的文本只是普通属性值，``(`` 打开的子变化整棵跳过，都不会
被当成落子。坐标为 SGF 标准：第一个字母是列，第二个字母是行，左上角为
``aa``。
"""

from __future__ import annotations

import re
import uuid
from datetime import timezone
from typing import Any, Optional, Sequence

from gomoku.shared.game_model import GameModel
from gomoku.shared.record import NON_BOARD_ENDINGS, GameRecord, RecordError
from gomoku.shared.rules import (
    BOARD_SIZE,
    Move,
    Player,
    in_bounds,
    parse_rule_mode,
)
from gomoku.shared.timeutil import parse_utc_timestamp

_SGF_LETTERS = "abcdefghijklmnopqrstuvwxyz"

#: SGF 缺少或写坏 DT 时使用的固定占位时间。用 now() 会让同一份文件
#: 每次导入得到不同的 created_at，导入结果无法复现。
DEFAULT_CREATED_AT = "1970-01-01T00:00:00+00:00"


class SgfError(ValueError):
    """SGF 内容非法或包含不支持的结构。"""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("]", "\\]")


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _coord(row: int, col: int) -> str:
    if not in_bounds(row, col):
        raise SgfError("落子坐标超出棋盘")
    return f"{_SGF_LETTERS[col]}{_SGF_LETTERS[row]}"


def _result_property(record: GameRecord) -> str:
    if record.winner is None:
        return "0"
    prefix = "B+" if record.winner is Player.BLACK else "W+"
    if record.result_reason in NON_BOARD_ENDINGS:
        suffix = {
            "resign": "R",
            "timeout": "T",
            "escape": "F",
        }[record.result_reason]
        return prefix + suffix
    return prefix


def record_to_sgf(record: GameRecord) -> str:
    """把一局棋谱序列化为 SGF 文本。

    规则模式写入标准 ``RU`` 属性：``RU[freestyle]`` 或 ``RU[renju]``。
    棋谱 ``metadata`` 里的 ``colophon`` 写成根节点的 ``C[]`` 注释，
    这样题跋能随棋谱一起流转，别的 SGF 软件也能看到。
    """
    if record.mode not in {"local", "ai", "puzzle", "online"}:
        raise SgfError("棋谱模式非法")
    try:
        rule_mode = parse_rule_mode(record.rule_mode).value
    except ValueError as exc:
        raise SgfError("棋谱规则模式非法") from exc
    date_text = record.created_at[:10]
    root = (
        "(;GM[1]FF[4]CA[UTF-8]AP[Gomoku:1.0]"
        f"SZ[{BOARD_SIZE}]"
        f"PB[{_escape(record.black_name or '黑方')}]"
        f"PW[{_escape(record.white_name or '白方')}]"
        f"DT[{_escape(date_text)}]"
        f"RE[{_result_property(record)}]"
        f"RU[{rule_mode}]"
    )
    # 题跋写成根节点的 C[] 注释。别的 SGF 软件会把这段显示成注释，
    # 题跋因此能跟着棋谱一起流转。
    colophon = str(record.metadata.get("colophon", "")).strip()
    if colophon:
        root += f"C[{_escape(colophon)}]"
    moves = []
    for move in record.moves:
        color = "B" if move.player is Player.BLACK else "W"
        moves.append(f";{color}[{_coord(move.row, move.col)}]")
    return root + "".join(moves) + ")\n"


def sgf_collection(games: Sequence[str]) -> str:
    """把多局 SGF 文本合成一个 SGF 集。

    SGF 集就是一串 ``(;...)`` 游戏；本函数只负责把已经生成好的单局
    文本拼起来，不解析内容。
    """
    parts: list[str] = []
    for game in games:
        text = game.strip()
        if not text:
            continue
        parts.append(text)
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def _parse_date(value: str) -> str:
    """把 SGF 的 DT 属性解析成 ISO 时间串。

    缺少或无法解析时返回固定占位值，保证导入结果可复现。
    """
    text = value.strip()
    if not text:
        return DEFAULT_CREATED_AT
    parsed = parse_utc_timestamp(text)
    if parsed is None:
        # SGF 允许 “2026-11-23,12:00:00” 这类写法，退回到只取日期部分。
        parsed = parse_utc_timestamp(text[:10])
    if parsed is None:
        return DEFAULT_CREATED_AT
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


def _read_prop_value(text: str, start: int) -> tuple[str, int]:
    """读取 ``[...]`` 属性值，返回 (原始值, 结束位置)。

    ``start`` 指向左方括号。返回值保留转义序列，由 :func:`_unescape` 统一
    处理，行为与旧实现一致。
    """
    index = start + 1
    length = len(text)
    chunks: list[str] = []
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            chunks.append(text[index : index + 2])
            index += 2
            continue
        if char == "]":
            return "".join(chunks), index + 1
        chunks.append(char)
        index += 1
    raise SgfError("SGF 属性值缺少右方括号")


def _skip_variation(text: str, start: int) -> int:
    """跳过 ``( ... )`` 子变化的整棵子树，返回结束位置。"""
    index = start
    depth = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "[":
            _value, index = _read_prop_value(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise SgfError("SGF 括号不匹配")


def _parse_main_variation(
    text: str,
) -> list[list[tuple[str, tuple[str, ...]]]]:
    """按 SGF 语法解析主变化，返回每个节点的属性列表。

    只跟随每个节点的第一个后继节点：``(`` 打开的子变化整棵跳过；注释
    （``C[...]``）里的 ``;B[aa]`` 只是普通属性值，不会被当成落子。

    按节点分组是必要的：根节点的 ``C[]`` 是题跋，而落在某一手上的
    ``C[]`` 只是那一步的普通注释。混在一起会把别家 SGF 软件的随手
    备注当成玩家写的题跋。
    """
    index = 0
    length = len(text)
    while index < length and text[index].isspace():
        index += 1
    if index >= length or text[index] != "(":
        raise SgfError("不是有效的 SGF 文本")
    index += 1

    nodes: list[list[tuple[str, tuple[str, ...]]]] = []
    while True:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            raise SgfError("SGF 文本意外结束")
        char = text[index]
        if char == ")":
            return nodes
        if char != ";":
            raise SgfError("SGF 语法错误：节点应以 ';' 开头")
        index += 1

        node: list[tuple[str, tuple[str, ...]]] = []
        nodes.append(node)
        while True:
            while index < length and text[index].isspace():
                index += 1
            if index >= length:
                raise SgfError("SGF 文本意外结束")
            char = text[index]
            if char in ";)":
                break
            if char == "(":
                # 子变化：整棵跳过，只保留主变化上的落子。
                index = _skip_variation(text, index)
                continue
            start = index
            while index < length and text[index].isalpha():
                index += 1
            key = text[start:index].upper()
            if not key:
                raise SgfError("SGF 语法错误：缺少属性标识符")
            raw_values, index = _read_prop_values(text, index, key)
            node.append((key, raw_values))

    return nodes


def _read_prop_values(
    text: str, start: int, key: str
) -> tuple[tuple[str, ...], int]:
    """读取一个属性的全部取值，返回 (取值列表, 结束位置)。

    SGF 允许一个属性带多个取值（例如让子棋的多值 ``B`` 属性），这里全部
    读出来；落子只取第一个取值，与旧实现的行为一致。
    """
    index = start
    length = len(text)
    values: list[str] = []
    while True:
        while index < length and text[index].isspace():
            index += 1
        if index >= length or text[index] != "[":
            raise SgfError(f"SGF 属性 {key} 缺少取值")
        value, index = _read_prop_value(text, index)
        values.append(value)
        mark = index
        while index < length and text[index].isspace():
            index += 1
        if index < length and text[index] == "[":
            continue
        index = mark
        return tuple(values), index


def _parse_moves(
    nodes: list[list[tuple[str, tuple[str, ...]]]]
) -> list[Move]:
    """从主变化的各节点属性里取出落子序列。"""
    moves: list[Move] = []
    for node in nodes:
        for key, values in node:
            if key not in ("B", "W") or not values:
                continue
            coord = values[0].strip().lower()
            if (
                len(coord) != 2
                or not coord.isascii()
                or not coord.isalpha()
            ):
                raise SgfError(f"落子坐标非法：{values[0]}")
            col = _SGF_LETTERS.find(coord[0])
            row = _SGF_LETTERS.find(coord[1])
            if (
                col < 0
                or row < 0
                or col >= BOARD_SIZE
                or row >= BOARD_SIZE
            ):
                raise SgfError("落子坐标超出棋盘")
            moves.append(
                Move(
                    row=row,
                    col=col,
                    player=Player.BLACK if key == "B" else Player.WHITE,
                )
            )
    return moves


def record_from_sgf(text: str) -> GameRecord:
    """从单主变化 SGF 文本构造并校验 ``GameRecord``。"""
    if not isinstance(text, str) or "(" not in text or ";" not in text:
        raise SgfError("不是有效的 SGF 文本")

    nodes = _parse_main_variation(text)

    properties: dict[str, str] = {}
    for node in nodes:
        for key, raw_values in node:
            if key in {"B", "W"} or not raw_values:
                continue
            properties.setdefault(key, _unescape(raw_values[0]))

    # 题跋只认根节点的 C[]：落在某一手上的 C[] 是那一步的普通注释，
    # 不能当成玩家写的题跋——否则别家 SGF 软件随手写的备注会被当成
    # 题跋存进册子，再导出时又被搬到根节点上。
    root_colophon = ""
    if nodes:
        for key, raw_values in nodes[0]:
            if key == "C" and raw_values:
                root_colophon = _unescape(raw_values[0]).strip()
                break

    size_text = properties.get("SZ", str(BOARD_SIZE))
    try:
        size = int(size_text.split(":", 1)[0])
    except ValueError as exc:
        raise SgfError("SZ 必须是整数") from exc
    if size != BOARD_SIZE:
        raise SgfError(f"只支持 {BOARD_SIZE} 路棋盘")

    moves = _parse_moves(nodes)

    # 规则模式是可选的 RU 属性；缺省按自由规则处理。未知取值说明文件
    # 使用了本程序不支持的规则，必须明确拒绝而不是猜测。
    try:
        rule_mode = parse_rule_mode(
            properties.get("RU", "freestyle")
        ).value
    except ValueError as exc:
        raise SgfError("RU 必须是 freestyle 或 renju") from exc

    try:
        model = GameModel.from_moves(moves, mode=rule_mode)
    except ValueError as exc:
        raise SgfError(f"落子序列非法：{exc}") from exc

    re_winner, suffix = _parse_re(properties.get("RE", ""))
    if re_winner is not None and model.winner is not None:
        if re_winner is not model.winner:
            raise SgfError("RE 胜者与棋盘最终局面不一致")
    winner = re_winner or model.winner

    if winner is None:
        mode = "local"
        # 没有胜负信息：满盘是和棋，否则是一份“还没下完”的快照。
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

    metadata: dict[str, Any] = {"source": "sgf"}
    # 根节点的 C[] 注释是题跋：导入时保留下来，题跋才能跟着棋谱走。
    if root_colophon:
        metadata["colophon"] = root_colophon

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
        "rule_mode": rule_mode,
        "metadata": metadata,
    }
    try:
        return GameRecord.from_dict(data)
    except RecordError as exc:
        raise SgfError(f"棋谱校验失败：{exc}") from exc


__all__ = [
    "DEFAULT_CREATED_AT",
    "SgfError",
    "record_from_sgf",
    "record_to_sgf",
    "sgf_collection",
]
