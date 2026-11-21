"""服务端管理命令。

从项目根目录执行（使用服务端独立 venv）：
    server/.venv/Scripts/python.exe -m server.manage create-invite --expires-days 30
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets

from sqlalchemy import select, update

from server.app.auth_service import create_invite
from server.app.config import Settings
from server.app.db import (
    create_engine,
    create_session_factory,
    create_tables,
    utcnow_naive,
)
from server.app.models import RefreshSession, User
from server.app.security import (
    hash_password,
    normalize_username,
    validate_password,
)


async def _create_invite(expires_days: int) -> str:
    settings = Settings.from_env()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    await create_tables(engine)
    async with factory() as session:
        code = await create_invite(session, settings, expires_days)
    await engine.dispose()
    return code


async def _reset_password(username: str, new_password: str | None) -> str:
    settings = Settings.from_env()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    await create_tables(engine)
    try:
        _, username_key = normalize_username(username)
        async with factory() as session:
            user = await session.scalar(
                select(User).where(User.username_key == username_key)
            )
            if user is None:
                raise RuntimeError("账号不存在")

            password = new_password or secrets.token_urlsafe(12)
            if not validate_password(password):
                raise RuntimeError("新密码必须为 8～128 个字符")
            user.password_hash = await hash_password(password)
            await session.execute(
                update(RefreshSession)
                .where(
                    RefreshSession.user_id == user.id,
                    RefreshSession.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow_naive())
            )
            await session.commit()
            return password
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gomoku 服务端管理工具")
    sub = parser.add_subparsers(dest="command", required=True)
    invite_parser = sub.add_parser("create-invite", help="生成一次性邀请码")
    invite_parser.add_argument(
        "--expires-days", type=int, default=30, help="有效期天数，默认 30"
    )
    reset_parser = sub.add_parser(
        "reset-password",
        help="管理员重置密码并撤销该账号所有 refresh 会话",
    )
    reset_parser.add_argument("--username", required=True)
    reset_parser.add_argument(
        "--new-password",
        default=None,
        help="不提供则生成随机密码并打印",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command == "create-invite":
        code = asyncio.run(_create_invite(max(1, args.expires_days)))
        print(code)
        return 0
    if args.command == "reset-password":
        try:
            password = asyncio.run(
                _reset_password(args.username, args.new_password)
            )
        except RuntimeError as exc:
            print(f"失败：{exc}")
            return 1
        print(password)
        return 0
    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
