"""好友申请、好友列表与删除好友接口。"""

from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.social import (
    FRIEND_EVENT_ACCEPTED,
    FRIEND_EVENT_DECLINED,
    FRIEND_EVENT_REMOVED,
    FRIEND_EVENT_REQUEST,
)
from server.app.api.social_helpers import (
    avatar_of,
    avatar_version_of,
    friendship_between,
    is_online,
    ordered_pair,
    push_friend_event,
)
from server.app.db import utcnow_naive
from server.app.dependencies import get_current_user, get_db
from server.app.models import DirectMessage, Friendship, User
from server.app.ratings import rank_for_rating
from server.app.schemas import (
    FriendEntry,
    FriendRequestCreate,
    FriendRequestEntry,
    FriendsResponse,
)

router = APIRouter(tags=["friends"])


def _friend_entry(
    user: User, online: bool, unread: int = 0
) -> FriendEntry:
    return FriendEntry(
        user_id=user.id,
        username=user.username,
        avatar=avatar_of(user),
        avatar_version=avatar_version_of(user),
        rating=user.rating,
        rank_name=rank_for_rating(user.rating).name,
        online=online,
        unread=unread,
    )


def _request_entry(
    friendship: Friendship, user: User
) -> FriendRequestEntry:
    return FriendRequestEntry(
        request_id=friendship.id,
        user_id=user.id,
        username=user.username,
        avatar=avatar_of(user),
        avatar_version=avatar_version_of(user),
        rating=user.rating,
        rank_name=rank_for_rating(user.rating).name,
        created_at=friendship.created_at,
    )


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


@router.get("/friends", response_model=FriendsResponse)
async def list_friends(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FriendsResponse:
    """当前账号的好友、收到的申请与发出的申请。"""
    rows = (
        await session.scalars(
            select(Friendship).where(
                or_(
                    Friendship.user_a == current_user.id,
                    Friendship.user_b == current_user.id,
                )
            )
        )
    ).all()
    other_ids = {
        row.user_b if row.user_a == current_user.id else row.user_a
        for row in rows
    }
    users = await _load_users(session, other_ids)

    unread_rows = await session.execute(
        select(DirectMessage.sender_id, func.count(DirectMessage.id))
        .where(
            DirectMessage.recipient_id == current_user.id,
            DirectMessage.read_at.is_(None),
        )
        .group_by(DirectMessage.sender_id)
    )
    unread = {sender: count for sender, count in unread_rows.all()}

    friends: list[FriendEntry] = []
    incoming: list[FriendRequestEntry] = []
    outgoing: list[FriendRequestEntry] = []

    for row in rows:
        other_id = (
            row.user_b if row.user_a == current_user.id else row.user_a
        )
        other = users.get(other_id)
        if other is None:
            continue
        if row.status == "accepted":
            friends.append(
                _friend_entry(
                    other,
                    is_online(request, other_id),
                    int(unread.get(other_id, 0)),
                )
            )
        elif row.status == "pending":
            entry = _request_entry(row, other)
            if row.requested_by == current_user.id:
                outgoing.append(entry)
            else:
                incoming.append(entry)

    friends.sort(key=lambda item: (not item.online, item.username))
    incoming.sort(key=lambda item: item.created_at, reverse=True)
    outgoing.sort(key=lambda item: item.created_at, reverse=True)
    return FriendsResponse(
        friends=friends, incoming=incoming, outgoing=outgoing
    )


@router.post("/friends/requests", response_model=FriendRequestEntry)
async def create_friend_request(
    payload: FriendRequestCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FriendRequestEntry:
    """向指定用户发送好友申请。"""
    target = await session.get(User, payload.user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能添加自己为好友")

    existing = await friendship_between(session, current_user.id, target.id)
    if existing is not None:
        if existing.status == "accepted":
            raise HTTPException(status_code=400, detail="你们已经是好友")
        if existing.requested_by == current_user.id:
            raise HTTPException(status_code=400, detail="好友申请已发送")
        raise HTTPException(
            status_code=409, detail="对方已向你发送好友申请，请先处理"
        )

    low, high = ordered_pair(current_user.id, target.id)
    friendship = Friendship(
        user_a=low,
        user_b=high,
        requested_by=current_user.id,
        status="pending",
        updated_at=utcnow_naive(),
    )
    session.add(friendship)
    try:
        await session.commit()
    except IntegrityError as exc:
        # 并发双击“加好友”会撞上 uq_friendships_pair 唯一约束。
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="好友申请已发送"
        ) from exc
    await session.refresh(friendship)

    await push_friend_event(
        request,
        target.id,
        FRIEND_EVENT_REQUEST,
        current_user,
        request_id=friendship.id,
    )
    return _request_entry(friendship, target)


async def _load_pending_addressed(
    session: AsyncSession,
    request_id: str,
    current_user: User,
) -> Friendship:
    friendship = await session.get(Friendship, request_id)
    if (
        friendship is None
        or friendship.status != "pending"
        or current_user.id
        not in (friendship.user_a, friendship.user_b)
    ):
        raise HTTPException(status_code=404, detail="好友申请不存在或已处理")
    if friendship.requested_by == current_user.id:
        raise HTTPException(
            status_code=400, detail="不能处理自己发出的好友申请"
        )
    return friendship


@router.post(
    "/friends/requests/{request_id}/accept",
    response_model=FriendEntry,
)
async def accept_friend_request(
    request_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FriendEntry:
    """接受收到的好友申请。"""
    friendship = await _load_pending_addressed(
        session, request_id, current_user
    )

    # 先取申请人再落库：否则“提交后才 404”会让客户端以为失败，好友
    # 关系却已经生效，双方状态对不上。
    requester = await session.get(User, friendship.requested_by)
    if requester is None:
        raise HTTPException(status_code=404, detail="申请用户不存在")

    # 条件更新：只有仍处于 pending 的那一行会被改写。与“拒绝”并发时，
    # 两个请求里只有一个能改到这行，另一个拿到干净的 404，而不会把已经
    # 建立的好友关系悄悄删掉。
    result = await session.execute(
        update(Friendship)
        .where(
            Friendship.id == request_id,
            Friendship.status == "pending",
        )
        .values(status="accepted", updated_at=utcnow_naive())
    )
    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(
            status_code=404, detail="好友申请不存在或已处理"
        )
    await session.commit()

    await push_friend_event(
        request, requester.id, FRIEND_EVENT_ACCEPTED, current_user
    )
    manager = getattr(request.app.state, "room_manager", None)
    if manager is not None:
        await manager.refresh_friend_cache(current_user.id)
        await manager.refresh_friend_cache(requester.id)
    return _friend_entry(
        requester, is_online(request, requester.id)
    )


@router.post(
    "/friends/requests/{request_id}/decline",
    status_code=204,
)
async def decline_friend_request(
    request_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """拒绝收到的好友申请。"""
    friendship = await _load_pending_addressed(
        session, request_id, current_user
    )
    requester_id = friendship.requested_by

    # 与“接受”一样用条件删除：只有仍处于 pending 的行才会被删掉。
    # 并发接受时删不到行，返回 404，而不是把已建立的好友关系抹掉。
    result = await session.execute(
        delete(Friendship).where(
            Friendship.id == request_id,
            Friendship.status == "pending",
        )
    )
    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(
            status_code=404, detail="好友申请不存在或已处理"
        )
    await session.commit()

    requester = await session.get(User, requester_id)
    if requester is not None:
        await push_friend_event(
            request, requester.id, FRIEND_EVENT_DECLINED, current_user
        )
    return Response(status_code=204)


@router.delete("/friends/{user_id}", status_code=204)
async def remove_friend(
    user_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """删除一位好友（双向生效）。"""
    friendship = await friendship_between(session, current_user.id, user_id)
    if friendship is None or friendship.status != "accepted":
        raise HTTPException(status_code=404, detail="好友关系不存在")

    # 条件删除：并发“接受申请”时删不到行，不会把刚建立的关系抹掉。
    result = await session.execute(
        delete(Friendship).where(
            Friendship.id == friendship.id,
            Friendship.status == "accepted",
        )
    )
    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(status_code=404, detail="好友关系不存在")
    await session.commit()

    other = await session.get(User, user_id)
    if other is not None:
        await push_friend_event(
            request, other.id, FRIEND_EVENT_REMOVED, current_user
        )
    manager = getattr(request.app.state, "room_manager", None)
    if manager is not None:
        await manager.refresh_friend_cache(current_user.id)
        await manager.refresh_friend_cache(user_id)
    return Response(status_code=204)


__all__ = ["router"]
