"""好友与私聊消息的共享校验。

房间内聊天使用 :mod:`gomoku.shared.chat`；私聊消息复用其文本净化逻辑，
但允许更长的正文。本模块不依赖 Qt，客户端与服务端都可导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.chat import sanitize_comment

#: 单条私聊消息的最大字符数。
MAX_DIRECT_MESSAGE_LENGTH = 200

FRIEND_EVENT_REQUEST = "request"
FRIEND_EVENT_ACCEPTED = "accepted"
FRIEND_EVENT_DECLINED = "declined"
FRIEND_EVENT_REMOVED = "removed"
FRIEND_EVENT_ONLINE = "online"
FRIEND_EVENT_OFFLINE = "offline"

FRIEND_EVENTS = frozenset(
    {
        FRIEND_EVENT_REQUEST,
        FRIEND_EVENT_ACCEPTED,
        FRIEND_EVENT_DECLINED,
        FRIEND_EVENT_REMOVED,
        FRIEND_EVENT_ONLINE,
        FRIEND_EVENT_OFFLINE,
    }
)


class SocialError(ValueError):
    """好友/私聊数据非法。"""


def normalize_direct_message(content: Any) -> str:
    """净化并校验私聊正文；非法时抛出 :class:`SocialError`。"""
    if not isinstance(content, str):
        raise SocialError("消息格式非法")
    text = sanitize_comment(content)
    if not text:
        raise SocialError("消息不能为空")
    if len(text) > MAX_DIRECT_MESSAGE_LENGTH:
        raise SocialError(
            f"消息不能超过 {MAX_DIRECT_MESSAGE_LENGTH} 个字符"
        )
    return text


@dataclass(frozen=True)
class DirectMessage:
    """一条已持久化的私聊消息。"""

    id: str
    sender_id: str
    sender_name: str
    sender_avatar: str
    recipient_id: str
    content: str
    sent_at_ms: int
    sender_avatar_version: int = 0


def parse_direct_message(payload: Any) -> DirectMessage:
    """解析服务端推送的私聊消息负载。"""
    if not isinstance(payload, dict):
        raise SocialError("私聊消息必须是对象")

    message_id = payload.get("id")
    sender_id = payload.get("sender_id")
    sender_name = payload.get("sender_name")
    recipient_id = payload.get("recipient_id")
    content = payload.get("content")
    sent_at_ms = payload.get("sent_at_ms", 0)

    if not isinstance(message_id, str) or not message_id:
        raise SocialError("私聊消息缺少 id")
    if not isinstance(sender_id, str) or not sender_id:
        raise SocialError("私聊消息缺少发送者")
    if not isinstance(sender_name, str) or not sender_name:
        raise SocialError("私聊消息缺少发送者名称")
    if not isinstance(recipient_id, str) or not recipient_id:
        raise SocialError("私聊消息缺少接收者")
    if not isinstance(content, str) or not content:
        raise SocialError("私聊消息正文为空")
    if len(content) > MAX_DIRECT_MESSAGE_LENGTH:
        raise SocialError("私聊消息过长")
    if not isinstance(sent_at_ms, int) or isinstance(sent_at_ms, bool):
        sent_at_ms = 0
    sender_avatar_version = payload.get("sender_avatar_version", 0)
    if (
        not isinstance(sender_avatar_version, int)
        or isinstance(sender_avatar_version, bool)
        or sender_avatar_version < 0
    ):
        sender_avatar_version = 0

    return DirectMessage(
        id=message_id,
        sender_id=sender_id,
        sender_name=sender_name[:32],
        sender_avatar=normalize_avatar(payload.get("sender_avatar"), sender_id),
        recipient_id=recipient_id,
        content=content,
        sent_at_ms=sent_at_ms,
        sender_avatar_version=sender_avatar_version,
    )


@dataclass(frozen=True)
class FriendEvent:
    """服务端推送的好友/在线状态事件。"""

    event: str
    user_id: str
    username: str
    avatar: str
    avatar_version: int = 0
    rating: int = 0
    rank_name: str = ""
    request_id: Optional[str] = None
    online: Optional[bool] = None


def parse_friend_event(payload: Any) -> FriendEvent:
    """解析服务端推送的好友事件负载。"""
    if not isinstance(payload, dict):
        raise SocialError("好友事件必须是对象")
    event = payload.get("event")
    if event not in FRIEND_EVENTS:
        raise SocialError("未知好友事件")
    user = payload.get("user")
    if not isinstance(user, dict):
        raise SocialError("好友事件缺少用户信息")
    user_id = user.get("id")
    username = user.get("username")
    if not isinstance(user_id, str) or not user_id:
        raise SocialError("好友事件缺少用户 id")
    if not isinstance(username, str) or not username:
        raise SocialError("好友事件缺少用户名")
    rating = user.get("rating", 0)
    if not isinstance(rating, int) or isinstance(rating, bool):
        rating = 0
    rank_name = user.get("rank_name", "")
    if not isinstance(rank_name, str):
        rank_name = ""
    request_id = payload.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id
    ):
        request_id = None
    online = payload.get("online")
    if online is not None and not isinstance(online, bool):
        online = None
    avatar_version = user.get("avatar_version", 0)
    if (
        not isinstance(avatar_version, int)
        or isinstance(avatar_version, bool)
        or avatar_version < 0
    ):
        avatar_version = 0

    return FriendEvent(
        event=event,
        user_id=user_id,
        username=username[:32],
        avatar=normalize_avatar(user.get("avatar"), user_id),
        avatar_version=avatar_version,
        rating=rating,
        rank_name=rank_name[:16],
        request_id=request_id,
        online=online,
    )


__all__ = [
    "MAX_DIRECT_MESSAGE_LENGTH",
    "FRIEND_EVENT_REQUEST",
    "FRIEND_EVENT_ACCEPTED",
    "FRIEND_EVENT_DECLINED",
    "FRIEND_EVENT_REMOVED",
    "FRIEND_EVENT_ONLINE",
    "FRIEND_EVENT_OFFLINE",
    "FRIEND_EVENTS",
    "SocialError",
    "normalize_direct_message",
    "DirectMessage",
    "parse_direct_message",
    "FriendEvent",
    "parse_friend_event",
]
