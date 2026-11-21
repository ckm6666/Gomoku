"""账号注册、登录、刷新、登出与当前用户接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.auth_service import (
    make_access_token,
    make_refresh_session,
)
from server.app.db import utcnow_naive
from server.app.dependencies import get_current_user, get_db
from server.app.models import RefreshSession, User
from server.app.schemas import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    UserResponse,
)
from server.app.security import (
    LoginBackoff,
    hash_password,
    normalize_username,
    sha256_hex,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """账号注册；用户名唯一。"""
    display_name, username_key = normalize_username(payload.username)
    settings = request.app.state.settings

    # 哈希计算放线程池，并且放在注册全局锁之外以减少锁持有时间。
    password_hash = await hash_password(payload.password)

    async with request.app.state.registration_lock:
        existing = await session.scalar(
            select(User.id).where(User.username_key == username_key)
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="用户名已被使用",
            )

        user = User(
            username=display_name,
            username_key=username_key,
            password_hash=password_hash,
        )
        session.add(user)
        try:
            await session.flush()

            refresh_record, raw_refresh = make_refresh_session(
                user.id, settings
            )
            session.add(refresh_record)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="用户名已被使用",
            ) from exc

        await session.refresh(user)
        access_token, expires_in = make_access_token(user, settings)
        return AuthResponse(
            access_token=access_token,
            refresh_token=raw_refresh,
            expires_in=expires_in,
            user=UserResponse.model_validate(user),
        )


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """账号密码登录；失败响应保持泛化，避免枚举账号。

    登录失败只返回通用的 401，**不按失败次数锁定账号或 IP**，避免正常
    用户因为多次输错密码而被拒绝登录。取而代之的是渐进退避：连续失败
    的请求会慢一点再返回（封顶 3 秒），把在线暴力猜测的速率压到不可行，
    同时不会把任何人挡在门外。
    """
    settings = request.app.state.settings
    _, username_key = normalize_username(payload.username)

    backoff: LoginBackoff = request.app.state.login_backoff
    backoff_key = f"{username_key}|{_client_ip(request)}"
    delay = await backoff.delay_for(backoff_key)
    if delay > 0:
        await asyncio.sleep(delay)

    user = await session.scalar(
        select(User).where(User.username_key == username_key)
    )
    password_ok = (
        user is not None
        and await verify_password(payload.password, user.password_hash)
    )
    if not password_ok or user is None or not user.is_active:
        await backoff.record_failure(backoff_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await backoff.clear(backoff_key)
    refresh_record, raw_refresh = make_refresh_session(user.id, settings)
    session.add(refresh_record)
    await session.commit()
    access_token, expires_in = make_access_token(user, settings)
    return AuthResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """轮换 refresh token 并签发新的 access token。"""
    settings = request.app.state.settings
    token_hash = sha256_hex(payload.refresh_token)
    now = utcnow_naive()

    record = await session.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == token_hash)
    )
    if record is None or record.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if record.revoked_at is not None:
        # 已轮换的 refresh token 再次出现：按令牌重放处理，撤销该用户所有会话。
        await session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.user_id == record.user_id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新令牌已失效，请重新登录",
        )

    user = await session.scalar(
        select(User).where(User.id == record.user_id, User.is_active.is_(True))
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号不存在或已停用",
        )

    new_record, raw_refresh = make_refresh_session(user.id, settings)
    record.revoked_at = now
    record.replaced_by_session_id = new_record.id
    session.add(new_record)
    await session.commit()

    access_token, expires_in = make_access_token(user, settings)
    return AuthResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """撤销当前用户提供的 refresh token；重复调用保持幂等。"""
    token_hash = sha256_hex(payload.refresh_token)
    await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.token_hash == token_hash,
            RefreshSession.user_id == current_user.id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow_naive())
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(current_user)


__all__ = ["router"]
