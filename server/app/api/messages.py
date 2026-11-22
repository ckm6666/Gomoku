"""好友私聊历史与发送接口。"""

from __future__ import annotations

import asyncio
import time
from datetime import timezone
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.social import (
    SocialError,
    normalize_direct_message,
)
from server.app.api.social_helpers import (
    are_friends,
    push_direct_message,
)
from server.app.db import utcnow_naive
from server.app.dependencies import get_current_user, get_db
from server.app.models import DirectMessage, User
from server.app.schemas import (
    DirectMessageEntry,
    DirectMessagesResponse,
    MarkMessagesReadRequest,
    MarkMessagesReadResponse,
    SendDirectMessageRequest,
)

router = APIRouter(tags=["messages"])

# 单进程内简单的私聊限流：每用户 10 秒最多 10 条。
_DM_WINDOW_SECONDS = 10.0
_DM_MAX_PER_WINDOW = 10
_send_times: dict[str, list[float]] = {}
_send_lock = asyncio.Lock()


async def _check_send_rate(user_id: str) -> None:
    now = time.monotonic()
    async with _send_lock:
        recent = [
            stamp
            for stamp in _send_times.get(user_id, [])
            if now - stamp < _DM_WINDOW_SECONDS
        ]
        if len(recent) >= _DM_MAX_PER_WINDOW:
            raise HTTPException(
                status_code=429, detail="发送过于频繁，请稍后再试"
            )
        recent.append(now)
        _send_times[user_id] = recent
        _prune_send_times(now)


#: 限流表里保留的用户数上限；超过后清理掉早已过期的键，避免字典无限增长。
_SEND_TRACKER_MAX_KEYS = 4096


def _prune_send_times(now: float) -> None:
    """丢弃长时间没有发送记录的用户键。"""
    if len(_send_times) <= _SEND_TRACKER_MAX_KEYS:
        return
    stale = [
        key
        for key, stamps in _send_times.items()
        if not stamps or now - stamps[-1] >= _DM_WINDOW_SECONDS
    ]
    for key in stale:
        _send_times.pop(key, None)


async def _require_friend(
    session: AsyncSession, user: User, other_id: str
) -> User:
    other = await session.get(User, other_id)
    if other is None or not other.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not await are_friends(session, user.id, other.id):
        raise HTTPException(status_code=403, detail="只能和好友私聊")
    return other


async def _load_users(
    session: AsyncSession, user_ids: Iterable[str]
) -> dict[str, User]:
    ids = set(user_ids)
    if not ids:
        return {}
    rows = (
        await session.scalars(select(User).where(User.id.in_(ids)))
    ).all()
    return {user.id: user for user in rows}


def _entry(
    message: DirectMessage, sender: User | None
) -> DirectMessageEntry:
    created = message.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return DirectMessageEntry(
        id=message.id,
        sender_id=message.sender_id,
        sender_name=sender.username if sender is not None else "好友",
        sender_avatar=normalize_avatar(
            sender.avatar if sender is not None else None,
            message.sender_id,
        ),
        recipient_id=message.recipient_id,
        content=message.content,
        created_at=message.created_at,
        sent_at_ms=int(created.timestamp() * 1000),
    )


@router.get(
    "/messages/{user_id}", response_model=DirectMessagesResponse
)
async def message_history(
    user_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DirectMessagesResponse:
    """读取与某位好友的最近私聊，并把对方发来的消息标记为已读。"""
    other = await _require_friend(session, current_user, user_id)

    rows = (
        await session.scalars(
            select(DirectMessage)
            .where(
                or_(
                    and_(
                        DirectMessage.sender_id == current_user.id,
                        DirectMessage.recipient_id == other.id,
                    ),
                    and_(
                        DirectMessage.sender_id == other.id,
                        DirectMessage.recipient_id == current_user.id,
                    ),
                )
            )
            .order_by(DirectMessage.created_at.desc())
            .limit(limit)
        )
    ).all()

    # 只把“这次真正返回给客户端”的消息标记为已读。否则对方发来 500 条
    # 未读、本次只取回 50 条时，剩下的 450 条会被悄悄标成已读——未读数
    # 归零，用户再也看不到它们。
    returned_ids = [
        message.id
        for message in rows
        if message.sender_id == other.id
        and message.recipient_id == current_user.id
    ]
    if returned_ids:
        await session.execute(
            update(DirectMessage)
            .where(
                DirectMessage.id.in_(returned_ids),
                DirectMessage.read_at.is_(None),
            )
            .values(read_at=utcnow_naive())
        )
        await session.commit()

    users = await _load_users(
        session, {message.sender_id for message in rows}
    )
    return DirectMessagesResponse(
        messages=[
            _entry(message, users.get(message.sender_id))
            for message in reversed(rows)
        ]
    )


@router.post(
    "/messages/{user_id}/read", response_model=MarkMessagesReadResponse
)
async def mark_messages_read(
    user_id: str,
    payload: MarkMessagesReadRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> MarkMessagesReadResponse:
    """把客户端已经显示过的消息标记为已读。

    读取历史时服务端已经顺带标记过一遍，但实时推送进来的消息不会：
    聊天窗开着时那条消息当场就读完了，如果不给客户端一个销账的入口，
    未读数会一直挂着，直到下一次拉历史才清零。
    """
    await _require_friend(session, current_user, user_id)
    ids = [
        message_id
        for message_id in payload.message_ids
        if isinstance(message_id, str) and message_id
    ]
    if not ids:
        return MarkMessagesReadResponse(read_count=0)
    result = await session.execute(
        update(DirectMessage)
        .where(
            DirectMessage.id.in_(ids),
            # 只认发给"我"、且出自对方的消息：别人的会话不能被顺手标掉。
            DirectMessage.recipient_id == current_user.id,
            DirectMessage.sender_id == user_id,
            DirectMessage.read_at.is_(None),
        )
        .values(read_at=utcnow_naive())
    )
    await session.commit()
    return MarkMessagesReadResponse(read_count=int(result.rowcount or 0))


@router.post(
    "/messages/{user_id}", response_model=DirectMessageEntry
)
async def send_message(
    user_id: str,
    payload: SendDirectMessageRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DirectMessageEntry:
    """向好友发送私聊；对方在线时实时推送，离线时落库等待补收。"""
    other = await _require_friend(session, current_user, user_id)
    try:
        content = normalize_direct_message(payload.content)
    except SocialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _check_send_rate(current_user.id)

    message = DirectMessage(
        sender_id=current_user.id,
        recipient_id=other.id,
        content=content,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)

    entry = _entry(message, current_user)
    await push_direct_message(
        request,
        other.id,
        entry.model_dump(mode="json"),
    )
    return entry


__all__ = ["router"]
