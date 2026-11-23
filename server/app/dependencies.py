"""FastAPI 依赖：数据库会话与当前登录用户。"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.errors import ProtocolError
from server.app.config import Settings
from server.app.models import User
from server.app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings: Settings = request.app.state.settings
    try:
        user_id = decode_access_token(credentials.credentials, settings)
    except ProtocolError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await session.scalar(
        select(User).where(User.id == user_id, User.is_active.is_(True))
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号不存在或已停用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_admin_user(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    """管理员依赖：用户名必须在 ``GOMOKU_ADMIN_USERNAMES`` 白名单中。"""
    settings: Settings = request.app.state.settings
    if current_user.username_key.lower() not in settings.admin_usernames:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


__all__ = ["get_db", "get_current_user", "get_admin_user"]
