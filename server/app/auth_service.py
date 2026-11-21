"""可复用的账号/邀请令牌辅助函数（HTTP、CLI、测试共用）。"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.config import Settings
from server.app.db import utcnow_naive
from server.app.models import InviteCode, RefreshSession, User
from server.app.security import (
    create_access_token,
    generate_invite_code,
    generate_refresh_token,
    sha256_hex,
)


async def create_invite(
    session: AsyncSession,
    settings: Settings,
    expires_days: int = 30,
) -> str:
    """生成并持久化一个一次性邀请码，返回给管理员打印的明文。"""
    for _ in range(5):
        code = generate_invite_code(settings.invite_code_length)
        code_hash = sha256_hex(code)
        exists = await session.scalar(
            select(InviteCode.id).where(InviteCode.code_hash == code_hash)
        )
        if exists:
            continue
        session.add(
            InviteCode(
                code_hash=code_hash,
                expires_at=utcnow_naive()
                + timedelta(days=max(1, expires_days)),
            )
        )
        await session.commit()
        return code
    raise RuntimeError("无法生成唯一邀请码，请重试")


def make_refresh_session(
    user_id: str,
    settings: Settings,
) -> tuple[RefreshSession, str]:
    """构造新的 refresh session 记录和返回给客户端的明文 token。"""
    raw_token, token_hash = generate_refresh_token()
    session = RefreshSession(
        id=str(uuid.uuid4()),
        user_id=user_id,
        token_hash=token_hash,
        expires_at=utcnow_naive()
        + timedelta(days=settings.refresh_token_days),
    )
    return session, raw_token


def make_access_token(user: User, settings: Settings) -> tuple[str, int]:
    return create_access_token(user.id, settings)


__all__ = [
    "create_invite",
    "make_refresh_session",
    "make_access_token",
]
