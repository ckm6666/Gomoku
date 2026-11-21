"""邀请制账号注册、登录、刷新、登出与当前用户接口。"""

from __future__ import annotations

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
from server.app.models import InviteCode, RefreshSession, User
from server.app.schemas import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    UserResponse,
)
from server.app.security import (
    hash_password,
    normalize_invite_code,
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
    """邀请码注册；邀请码只能使用一次。"""
    display_name, username_key = normalize_username(payload.username)
    invite_hash = sha256_hex(normalize_invite_code(payload.invite_code))
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

        invite = await session.scalar(
            select(InviteCode).where(
                InviteCode.code_hash == invite_hash,
                InviteCode.used_by.is_(None),
            )
        )
        now = utcnow_naive()
        if (
            invite is None
            or (invite.expires_at is not None and invite.expires_at <= now)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="邀请码无效、已使用或已过期",
            )

        user = User(
            username=display_name,
            username_key=username_key,
            password_hash=password_hash,
        )
        session.add(user)
        try:
            await session.flush()
            claimed = await session.execute(
                update(InviteCode)
                .where(
                    InviteCode.id == invite.id,
                    InviteCode.used_by.is_(None),
                )
                .values(used_by=user.id, used_at=now)
            )
            if (claimed.rowcount or 0) != 1:
                await session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="邀请码已被使用",
                )

            refresh_record, raw_refresh = make_refresh_session(
                user.id, settings
            )
            session.add(refresh_record)
            await session.commit()
        except HTTPException:
            raise
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="用户名或邀请码已被使用",
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
    """账号密码登录；失败响应保持泛化，避免枚举账号。"""
    settings = request.app.state.settings
    throttle = request.app.state.login_throttle
    _, username_key = normalize_username(payload.username)
    throttle_key = f"{username_key}|{_client_ip(request)}"

    decision = await throttle.check(throttle_key)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试过多，请稍后再试",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    user = await session.scalar(
        select(User).where(User.username_key == username_key)
    )
    password_ok = (
        user is not None
        and await verify_password(payload.password, user.password_hash)
    )
    if not password_ok or user is None or not user.is_active:
        await throttle.record_failure(throttle_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await throttle.clear(throttle_key)
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
