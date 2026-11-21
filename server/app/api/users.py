"""公开用户资料、用户搜索与头像更新接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.avatar import is_valid_avatar
from server.app.api.social_helpers import (
    friendship_between,
    avatar_of,
    is_online,
)
from server.app.dependencies import get_current_user, get_db
from server.app.models import User
from server.app.ratings import rank_for_rating
from server.app.schemas import (
    AvatarUpdateRequest,
    PublicProfileResponse,
    UserResponse,
    UserSearchEntry,
    UserSearchResponse,
)

router = APIRouter(tags=["users"])


def _search_entry(user: User, online: bool) -> UserSearchEntry:
    return UserSearchEntry(
        id=user.id,
        username=user.username,
        avatar=avatar_of(user),
        rating=user.rating,
        rank_name=rank_for_rating(user.rating).name,
        online=online,
    )


@router.get("/users/search", response_model=UserSearchResponse)
async def search_users(
    request: Request,
    q: str = Query(min_length=1, max_length=24),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserSearchResponse:
    """按用户名模糊搜索活跃账号（用于添加好友）。"""
    key = q.strip().casefold()
    if not key:
        return UserSearchResponse(entries=[], total=0)
    rows = (
        await session.scalars(
            select(User)
            .where(
                User.is_active.is_(True),
                User.username_key.contains(key, autoescape=True),
                User.id != current_user.id,
            )
            .order_by(User.rating.desc(), User.username_key.asc())
            .limit(20)
        )
    ).all()
    entries = [
        _search_entry(user, is_online(request, user.id)) for user in rows
    ]
    return UserSearchResponse(entries=entries, total=len(entries))


@router.patch("/users/me/avatar", response_model=UserResponse)
async def update_avatar(
    payload: AvatarUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """更新当前账号的预置头像。"""
    if not is_valid_avatar(payload.avatar):
        raise HTTPException(status_code=400, detail="未知头像")
    current_user.avatar = payload.avatar
    await session.commit()
    await session.refresh(current_user)
    return UserResponse.model_validate(current_user)


@router.get("/users/{user_id}", response_model=PublicProfileResponse)
async def public_profile(
    user_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PublicProfileResponse:
    """查看任意活跃用户的公开资料与好友关系状态。"""
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")

    if user.id == current_user.id:
        friendship_status = "self"
    else:
        friendship = await friendship_between(
            session, current_user.id, user.id
        )
        if friendship is None:
            friendship_status = "none"
        elif friendship.status == "accepted":
            friendship_status = "friends"
        elif friendship.requested_by == current_user.id:
            friendship_status = "pending_out"
        else:
            friendship_status = "pending_in"

    return PublicProfileResponse(
        user=UserResponse.model_validate(user),
        online=is_online(request, user.id),
        friendship=friendship_status,
    )


__all__ = ["router"]
