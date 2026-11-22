"""好友/私聊接口共用的查询与 WebSocket 推送辅助。"""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.protocol import (
    SERVER_DIRECT_MESSAGE,
    SERVER_FRIEND_EVENT,
    make_message,
)
from server.app.achievement_service import achievement_keys_for_user
from server.app.models import Friendship, User
from server.app.ratings import rank_for_rating


def ordered_pair(user_a: str, user_b: str) -> tuple[str, str]:
    """返回排序后的用户对，保证同一好友关系只有一条记录。"""
    return (user_a, user_b) if user_a <= user_b else (user_b, user_a)


async def friendship_between(
    session: AsyncSession,
    user_a: str,
    user_b: str,
) -> Optional[Friendship]:
    """查询两人之间的好友记录（accepted 或 pending）。"""
    low, high = ordered_pair(user_a, user_b)
    return await session.scalar(
        select(Friendship).where(
            Friendship.user_a == low,
            Friendship.user_b == high,
        )
    )


async def are_friends(
    session: AsyncSession, user_a: str, user_b: str
) -> bool:
    """两人是否为已接受的好友。"""
    friendship = await friendship_between(session, user_a, user_b)
    return friendship is not None and friendship.status == "accepted"


async def friend_ids(session: AsyncSession, user_id: str) -> list[str]:
    """返回某用户的全部好友 id。"""
    rows = (
        await session.scalars(
            select(Friendship).where(
                Friendship.status == "accepted",
                or_(
                    Friendship.user_a == user_id,
                    Friendship.user_b == user_id,
                ),
            )
        )
    ).all()
    return [
        row.user_b if row.user_a == user_id else row.user_a for row in rows
    ]


def avatar_of(user: User) -> str:
    """返回用户头像 key（兼容旧数据）。"""
    return normalize_avatar(user.avatar, user.id)


def avatar_version_of(user: User) -> int:
    """返回对外可见的头像版本号；没有自定义图片时为 0。"""
    return user.public_avatar_version


def user_payload(user: User) -> dict:
    """WebSocket 推送用的用户摘要。"""
    return {
        "id": user.id,
        "username": user.username,
        "avatar": avatar_of(user),
        "avatar_version": avatar_version_of(user),
        "achievements": list(achievement_keys_for_user(user)),
        "rating": user.rating,
        "rank_name": rank_for_rating(user.rating).name,
    }


def is_online(request: Request, user_id: str) -> bool:
    """查询用户是否有一条活动 WebSocket 连接。"""
    manager = getattr(request.app.state, "room_manager", None)
    return bool(manager is not None and manager.is_online(user_id))


async def push_to_user(
    request: Request, user_id: str, message: dict
) -> bool:
    """向指定用户的在线连接推送一条消息。"""
    manager = getattr(request.app.state, "room_manager", None)
    if manager is None:
        return False
    return await manager.send_to_user(user_id, message)


async def push_friend_event(
    request: Request,
    target_id: str,
    event: str,
    user: User,
    *,
    request_id: Optional[str] = None,
    online: Optional[bool] = None,
) -> bool:
    """推送好友申请/接受/删除/在线状态事件。"""
    payload: dict = {"event": event, "user": user_payload(user)}
    if request_id is not None:
        payload["request_id"] = request_id
    if online is not None:
        payload["online"] = online
    return await push_to_user(
        request, target_id, make_message(SERVER_FRIEND_EVENT, payload)
    )


async def push_direct_message(
    request: Request, target_id: str, message: dict
) -> bool:
    """推送一条私聊消息。"""
    return await push_to_user(
        request, target_id, make_message(SERVER_DIRECT_MESSAGE, message)
    )


__all__ = [
    "ordered_pair",
    "friendship_between",
    "are_friends",
    "friend_ids",
    "avatar_of",
    "avatar_version_of",
    "user_payload",
    "is_online",
    "push_to_user",
    "push_friend_event",
    "push_direct_message",
]
