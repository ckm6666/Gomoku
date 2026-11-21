"""可复用的账号/令牌辅助函数（HTTP、CLI、测试共用）。"""

from __future__ import annotations

import uuid
from datetime import timedelta

from server.app.config import Settings
from server.app.db import utcnow_naive
from server.app.models import RefreshSession, User
from server.app.security import (
    create_access_token,
    generate_refresh_token,
)


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
    "make_refresh_session",
    "make_access_token",
]
