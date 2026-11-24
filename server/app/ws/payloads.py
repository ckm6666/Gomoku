"""联机协议中纯载荷/状态消息的构造器。

这些函数只依赖房间实体、共享规则与设置有；不读 RoomManager 的任何
可变状态，也不发消息。``RoomManager`` 保留同名私有方法作为薄封装，
wire 字段与顺序保持不变。
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

from gomoku.shared.avatar import DEFAULT_AVATAR, normalize_avatar
from gomoku.shared.game_model import GameModel
from gomoku.shared.protocol import (
    SERVER_CHAT,
    SERVER_GAME_STATE,
    SERVER_OPPONENT_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CREATED,
    SERVER_UNDO_STATUS,
    make_message,
)
from gomoku.shared.rules import Player
from server.app.config import Settings
from server.app.ratings import rank_for_rating
from server.app.ws.entities import (
    ClientConnection,
    QueuedPlayer,
    Room,
    RoomInvite,
)


def color_text(player: Player) -> str:
    """把棋子颜色转成协议里的字符串。"""
    return "black" if player is Player.BLACK else "white"


def status_for_game(game: GameModel) -> str:
    """根据棋局结果给出房间状态字符串。"""
    if game.winner is Player.BLACK:
        return "black_won"
    if game.winner is Player.WHITE:
        return "white_won"
    if game.is_draw:
        return "draw"
    return "playing"


def connection_user_payload(conn: ClientConnection) -> dict:
    """连接用户的好友/邀请摘要。"""
    return {
        "id": conn.user_id,
        "username": conn.username,
        "avatar": normalize_avatar(conn.avatar, conn.user_id),
        "avatar_version": max(0, conn.avatar_version),
        "achievements": list(conn.achievements),
        "rating": conn.rating,
        "rank_name": rank_for_rating(conn.rating).name,
    }


def invite_user_payload(
    user_id: str, inviter: Optional[ClientConnection]
) -> dict:
    """邀请里展示的邀请人信息；对方已离线时退回占位值。"""
    if inviter is not None:
        return connection_user_payload(inviter)
    return {
        "id": user_id,
        "username": "好友",
        "avatar": DEFAULT_AVATAR,
        "rating": 0,
        "rank_name": "",
    }


def queue_state_payload(
    user_id: str, queue: Mapping[str, QueuedPlayer]
) -> dict:
    """匹配队列状态；未入队时返回 idle。"""
    queued = queue.get(user_id)
    rating = queued.rating if queued is not None else None
    return {
        "state": "queued" if queued is not None else "idle",
        "queue_size": len(queue),
        "your_rating": rating,
        "rank_name": (
            rank_for_rating(rating).name if rating is not None else ""
        ),
    }


def match_found_payload(
    room: Room, user_id: str, opponent_id: Optional[str]
) -> dict:
    """配对成功消息的载荷。"""
    color = room.color_for(user_id)
    return {
        "game_id": room.game_id,
        "your_color": color_text(color) if color else "black",
        "opponent_name": (
            room.username_for(opponent_id)
            if opponent_id
            else "对手"
        ),
        "your_rating": room.rating_before.get(user_id),
        "opponent_rating": (
            room.rating_before.get(opponent_id)
            if opponent_id
            else None
        ),
        "rated": True,
        "time_control": room.time_control.to_payload(),
    }


def room_created_message(room: Room) -> dict:
    """房主创建房间成功的消息。"""
    return make_message(
        SERVER_ROOM_CREATED,
        {
            "room_code": room.code,
            "game_id": room.game_id,
            "your_color": "black",
            "black_name": room.black_name,
            "white_name": room.white_name,
            "turn_seconds": (
                room.time_control.turn_seconds
                if not room.time_control.is_fischer
                else None
            ),
            "time_control": room.time_control.to_payload(),
            # 建房回执就带上规则模式，房主无需等第一份快照。
            "rule_mode": room.rule_mode.value,
        },
    )


def opponent_status_message(who_name: str, online: bool) -> dict:
    """对手上下线通知。"""
    return make_message(
        SERVER_OPPONENT_STATUS,
        {"username": who_name, "online": online},
    )


def rematch_status_message(room: Room, user_id: str) -> dict:
    """再战投票状态（按接收者视角）。"""
    opponent = room.opponent_id(user_id)
    return make_message(
        SERVER_REMATCH_STATUS,
        {
            "your_requested": user_id in room.rematch_votes,
            "opponent_requested": (
                opponent in room.rematch_votes
                if opponent
                else False
            ),
        },
    )


def undo_status_message(
    room: Room,
    state: str,
    requester_id: Optional[str],
    reason: str = "",
) -> dict:
    """悔棋状态消息。"""
    return make_message(
        SERVER_UNDO_STATUS,
        {
            "state": state,
            "requester_id": requester_id or "",
            "requester_name": (
                room.username_for(requester_id)
                if requester_id
                else ""
            ),
            "reason": reason,
        },
    )


def room_state_payload(
    room: Room,
    user_id: str,
    *,
    settings: Settings,
    remaining_ms: Callable[[Room, str], Optional[int]],
    now_ms: Callable[[], int],
) -> dict:
    """构造某位玩家/观战者看到的权威局面载荷。

    ``remaining_ms`` 读取时钟余量（Fischer 模式），``now_ms`` 提供
    服务器当前时间；两者由调用方注入，保持原有的取值时机。
    """
    color = room.color_for(user_id)
    # 观战者没有执子颜色，但仍然可以拿到完整的权威快照。
    is_spectator = False
    if color is None:
        if user_id not in room.spectators:
            raise RuntimeError("用户不属于该房间")
        is_spectator = True
    opponent_id = room.opponent_id(user_id)
    last_move = room.game.last_move
    status = room.status
    undo_limit = settings.undo_limit_per_game if color else 0
    used_undos = room.undo_counts.get(user_id, 0) if color else 0
    undo_remaining = max(0, undo_limit - used_undos)
    undo_cooldown_seconds = (
        int(settings.undo_cooldown_seconds + 0.999)
        if color and settings.undo_cooldown_seconds > 0
        else 0
    )
    undo_cooldown_until_ms: Optional[int] = None
    if color and settings.undo_cooldown_seconds > 0:
        last_undo = room.undo_last_request.get(user_id)
        if last_undo is not None:
            undo_cooldown_until_ms = int(
                (last_undo + settings.undo_cooldown_seconds) * 1000
            )
    return {
        "game_id": room.game_id,
        "board": [
            [
                None if cell is None else color_text(cell)
                for cell in row
            ]
            for row in room.game.board
        ],
        "current_player": color_text(room.game.current_player),
        "your_color": color_text(color) if color else None,
        "is_spectator": is_spectator,
        "move_no": room.game.move_count,
        "status": status,
        "state_version": room.state_version,
        "result_reason": room.result_reason or "",
        "undo_requester_id": room.undo_requester_id or "",
        "undo_remaining": undo_remaining,
        "undo_limit": undo_limit,
        "undo_cooldown_until_ms": undo_cooldown_until_ms,
        "undo_cooldown_seconds": undo_cooldown_seconds,
        "last_move": (
            None
            if last_move is None
            else {
                "row": last_move.row,
                "col": last_move.col,
                "player": color_text(last_move.player),
            }
        ),
        "winning_line": [list(cell) for cell in room.game.winning_line],
        "room_code": "" if room.matchmade else room.code,
        "black_name": room.black_name,
        "white_name": room.white_name,
        "black_user_id": room.black_user_id,
        "white_user_id": room.white_user_id or "",
        "black_avatar": room.avatar_for(room.black_user_id),
        "white_avatar": (
            room.avatar_for(room.white_user_id)
            if room.white_user_id
            else DEFAULT_AVATAR
        ),
        "black_avatar_version": room.avatar_version_for(
            room.black_user_id
        ),
        "white_avatar_version": (
            room.avatar_version_for(room.white_user_id)
            if room.white_user_id
            else 0
        ),
        "black_achievements": list(room.black_achievements),
        "white_achievements": list(room.white_achievements),
        "your_new_achievements": list(
            room.new_achievements.get(user_id, ())
        ),
        "turn_deadline_ms": room.turn_deadline_ms,
        "server_now_ms": now_ms(),
        "move_timeout_seconds": (
            None
            if room.time_control.is_fischer
            else room.time_control.turn_seconds
        ),
        "time_control": room.time_control.to_payload(),
        "black_remaining_ms": remaining_ms(room, room.black_user_id),
        "white_remaining_ms": (
            remaining_ms(room, room.white_user_id)
            if room.white_user_id
            else None
        ),
        "opponent_online": (
            # 观战者没有“对手”这个概念：报告双方是否都在线。
            (
                room.black_user_id in room.connections
                and room.white_user_id in room.connections
            )
            if is_spectator
            else (
                room.opponent_id(user_id) in room.connections
                if room.opponent_id(user_id) else False
            )
        ),
        "rated": room.rated,
        # 权威快照携带规则模式：客户端据此绘制禁手提示并校验落子预览。
        "rule_mode": room.rule_mode.value,
        "your_rating_before": room.rating_before.get(user_id),
        "your_rating_after": room.rating_after.get(user_id),
        "opponent_rating_before": (
            room.rating_before.get(opponent_id)
            if opponent_id is not None
            else None
        ),
        "opponent_rating_after": (
            room.rating_after.get(opponent_id)
            if opponent_id is not None
            else None
        ),
        "rating_delta": room.rating_delta.get(user_id),
    }


def game_state_message(
    room: Room,
    conn: ClientConnection,
    *,
    settings: Settings,
    remaining_ms: Callable[[Room, str], Optional[int]],
    now_ms: Callable[[], int],
    request_id: Optional[str] = None,
) -> Optional[dict]:
    """构造某条连接的权威局面消息；不属于该房间时返回 None。"""
    try:
        payload = room_state_payload(
            room,
            conn.user_id,
            settings=settings,
            remaining_ms=remaining_ms,
            now_ms=now_ms,
        )
    except RuntimeError:
        return None
    return make_message(
        SERVER_GAME_STATE, payload, request_id=request_id
    )


def invite_payload(
    invite: RoomInvite, inviter: Optional[ClientConnection]
) -> dict:
    """房间邀请消息的载荷。"""
    return {
        "invite_id": invite.invite_id,
        "inviter": invite_user_payload(invite.inviter_id, inviter),
        "rated": False,
        # 被邀请者在接受前就能看到本局规则模式。
        "rule_mode": invite.room.rule_mode.value,
    }


def invite_result_payload(
    invite: RoomInvite,
    status: str,
    accepted: Optional[bool],
    user: dict,
    reason: str = "",
) -> dict:
    """邀请结果消息的载荷。"""
    return {
        "invite_id": invite.invite_id,
        "status": status,
        "accepted": accepted,
        "user": user,
        "reason": reason,
    }


def chat_message(
    conn: ClientConnection,
    kind: str,
    content: str,
    sent_at_ms: int,
) -> dict:
    """房间聊天的广播消息。"""
    return make_message(
        SERVER_CHAT,
        {
            "kind": kind,
            "content": content,
            "sender_id": conn.user_id,
            "sender_name": conn.username,
            "sent_at_ms": sent_at_ms,
        },
    )


__all__ = [
    "chat_message",
    "color_text",
    "connection_user_payload",
    "game_state_message",
    "invite_payload",
    "invite_result_payload",
    "invite_user_payload",
    "match_found_payload",
    "opponent_status_message",
    "queue_state_payload",
    "rematch_status_message",
    "room_created_message",
    "room_state_payload",
    "status_for_game",
    "undo_status_message",
]
