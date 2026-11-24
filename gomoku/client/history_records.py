"""把服务端对局详情转换为本地可复盘的 :class:`GameRecord`。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gomoku.shared.record import GameRecord, RecordError

_EPOCH = "1970-01-01T00:00:00+00:00"


def record_from_game_detail(data: Mapping[str, Any]) -> GameRecord:
    """解析服务端 ``/games/{id}`` 响应；非法时抛出 ``RecordError``。"""
    if not isinstance(data, Mapping):
        raise RecordError("对局详情必须是对象")

    game_id = data.get("game_id")
    if not isinstance(game_id, str) or not game_id:
        raise RecordError("对局详情缺少 game_id")

    raw_moves = data.get("moves")
    if not isinstance(raw_moves, list):
        raise RecordError("对局详情缺少落子数组")

    moves: list[dict[str, Any]] = []
    for index, item in enumerate(raw_moves):
        if not isinstance(item, Mapping):
            raise RecordError(f"moves[{index}] 必须是对象")
        row = item.get("row")
        col = item.get("col")
        color = item.get("color")
        if color not in ("black", "white"):
            raise RecordError(f"moves[{index}] 颜色非法")
        moves.append({"row": row, "col": col, "player": color})

    winner_user_id = data.get("winner_user_id")
    black_user_id = data.get("black_user_id")
    white_user_id = data.get("white_user_id")
    winner: str | None = None
    if winner_user_id is not None:
        if winner_user_id == black_user_id:
            winner = "black"
        elif winner_user_id == white_user_id:
            winner = "white"
        else:
            raise RecordError("胜者不属于本局玩家")

    created_at = (
        data.get("created_at")
        or data.get("started_at")
        or data.get("ended_at")
        or _EPOCH
    )
    if not isinstance(created_at, str) or not created_at:
        created_at = _EPOCH

    # 规则模式交给共享棋谱层校验：缺省或显式为 null 时按自由规则处理，
    # 未知取值会抛出 RecordError，避免复盘时采用错误规则。
    rule_mode = data.get("rule_mode")
    if rule_mode is None:
        rule_mode = "freestyle"

    metadata: dict[str, Any] = {
        "status": data.get("status", ""),
        "your_color": data.get("your_color", ""),
    }
    rating_delta = data.get("rating_delta")
    if isinstance(rating_delta, int) and not isinstance(rating_delta, bool):
        metadata["rating_delta"] = rating_delta

    return GameRecord.from_dict(
        {
            "version": 1,
            "record_id": f"online-{game_id}",
            "created_at": created_at,
            "mode": "online",
            "winner": winner,
            "result_reason": data.get("result_reason") or "",
            "black_name": data.get("black_username", ""),
            "white_name": data.get("white_username", ""),
            "rated": bool(data.get("rated", False)),
            "rule_mode": rule_mode,
            "moves": moves,
            "metadata": metadata,
        }
    )


__all__ = ["record_from_game_detail"]
