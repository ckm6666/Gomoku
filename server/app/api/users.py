"""公开用户资料、用户搜索与头像更新接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.avatar import is_valid_avatar
from server.app.api.social_helpers import (
    friendship_between,
    avatar_of,
    avatar_version_of,
    is_online,
)
from server.app.avatar_service import (
    AvatarImageError,
    decode_avatar_image,
    validate_avatar_image,
)
from server.app.dependencies import get_current_user, get_db
from server.app.models import User
from server.app.ratings import rank_for_rating
from server.app.security import normalize_username
from server.app.schemas import (
    AvatarImageRequest,
    AvatarUpdateRequest,
    PublicProfileResponse,
    UserResponse,
    UserSearchEntry,
    UserSearchResponse,
)

router = APIRouter(tags=["users"])


async def _sync_online_avatar(request: Request, user: User) -> None:
    """把新头像同步给该用户的活动 WebSocket 连接与所在房间。"""
    manager = getattr(request.app.state, "room_manager", None)
    if manager is not None:
        await manager.update_connection_avatar(
            user.id, avatar_of(user), user.public_avatar_version
        )


def _search_entry(user: User, online: bool) -> UserSearchEntry:
    return UserSearchEntry(
        id=user.id,
        username=user.username,
        avatar=avatar_of(user),
        avatar_version=avatar_version_of(user),
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
    # 与注册时同一套规范化（NFKC + casefold），否则全角等宽字符注册出来的
    # 用户用同样的字符搜不到——屏幕上显示的名字和数据库里的 key 对不上。
    _, key = normalize_username(q)
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
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """更新当前账号的预置头像；选择印章会移除自定义图片。"""
    if not is_valid_avatar(payload.avatar):
        raise HTTPException(status_code=400, detail="未知头像")
    current_user.avatar = payload.avatar
    current_user.avatar_image = None
    current_user.has_avatar_image = False
    # 版本号只增不减：清空图片后旧客户端的缓存 URL 不会再次被复用。
    current_user.avatar_version += 1
    await session.commit()
    await session.refresh(current_user)
    await _sync_online_avatar(request, current_user)
    return UserResponse.model_validate(current_user)


@router.put("/users/me/avatar-image", response_model=UserResponse)
async def update_avatar_image(
    payload: AvatarImageRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """上传本地图片作为头像；JPEG/PNG，自动裁剪与体积由客户端完成。"""
    try:
        normalized = validate_avatar_image(payload.image)
    except AvatarImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current_user.avatar_image = normalized
    current_user.has_avatar_image = True
    current_user.avatar_version += 1
    await session.commit()
    await session.refresh(current_user)
    await _sync_online_avatar(request, current_user)
    return UserResponse.model_validate(current_user)


@router.delete("/users/me/avatar-image", response_model=UserResponse)
async def delete_avatar_image(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> UserResponse:
    """移除自定义头像，回落到当前选中的预置印章。"""
    current_user.avatar_image = None
    current_user.has_avatar_image = False
    current_user.avatar_version += 1
    await session.commit()
    await session.refresh(current_user)
    await _sync_online_avatar(request, current_user)
    return UserResponse.model_validate(current_user)


@router.get("/users/{user_id}/avatar")
async def user_avatar_image(
    user_id: str,
    session: AsyncSession = Depends(get_db),
) -> Response:
    """返回用户的自定义头像图片。

    头像是应用内公开资料，使用不可猜测的用户 UUID 作为路径；版本号由
    客户端放在查询串里参与缓存键，因此可以放心地长缓存。
    """
    avatar_image = await session.scalar(
        select(User.avatar_image).where(
            User.id == user_id, User.is_active.is_(True)
        )
    )
    if not avatar_image:
        raise HTTPException(status_code=404, detail="头像不存在")
    try:
        decoded = decode_avatar_image(avatar_image)
    except AvatarImageError as exc:
        raise HTTPException(status_code=404, detail="头像不存在") from exc
    return Response(
        content=decoded.data,
        media_type=decoded.media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


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
