"""联机页面共用的常量和空棋盘快照。"""

from __future__ import annotations

from typing import Optional

from gomoku.client.board_view import BoardView
from gomoku.shared.protocol import GameSnapshot
from gomoku.shared.rules import BOARD_SIZE, Cell, Player

#: 聊天输入框的常规占位提示。
CHAT_INPUT_HINT = "说点什么…（回车发送）"

_EMPTY_BOARD: tuple[tuple[Cell, ...], ...] = tuple(
    tuple(None for _ in range(BOARD_SIZE)) for _ in range(BOARD_SIZE)
)


def empty_view() -> BoardView:
    return BoardView(
        board=_EMPTY_BOARD,
        current_player=Player.BLACK,
        last_move=None,
        winner=None,
        winning_line=(),
        is_draw=False,
        is_game_over=False,
        move_count=0,
    )


def online_victory_notice(
    snapshot: Optional[GameSnapshot],
) -> Optional[tuple[str, str]]:
    """对手认输或逃跑且己方获胜时，返回胜利弹窗的标题与正文。"""
    if snapshot is None or not snapshot.is_game_over:
        return None
    if snapshot.winner is None or snapshot.winner is not snapshot.your_color:
        return None
    if snapshot.result_reason == "resign":
        return ("对手认输", "对方认输，你获胜！")
    if snapshot.result_reason == "escape":
        return ("对手逃跑", "对方逃跑，你获胜！")
    return None


def online_timeout_notice(
    snapshot: Optional[GameSnapshot],
) -> Optional[tuple[str, str]]:
    """超时判负时返回提示弹窗的标题与正文。

    超时的一方判负，对手获胜，因此双方都会看到弹窗：胜方看到“对手
    超时”，负方看到“本手超时”。不是超时结束的对局返回 None。
    """
    if snapshot is None or not snapshot.is_game_over:
        return None
    if snapshot.result_reason != "timeout":
        return None
    if snapshot.winner is None or snapshot.your_color is None:
        return None
    if snapshot.winner is snapshot.your_color:
        return ("对手超时", "对手本手超时未落子，你获胜！")
    return ("本手超时", "本手超时未落子，本局判负。")


__all__ = [
    "CHAT_INPUT_HINT",
    "empty_view",
    "online_timeout_notice",
    "online_victory_notice",
]
