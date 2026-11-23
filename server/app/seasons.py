"""排位赛季：周期滚动、积分软重置与赛季归档。

赛季由服务端统一推进，客户端只读取当前赛季编号/名称与剩余时间：

* 没有赛季时自动创建第 1 赛季；
* 当前赛季到期后，把所有参赛账号的最终积分写入 ``season_ratings``，
  然后按 :func:`server.app.glicko2.season_soft_reset` 软重置积分；
* 连续跨过多个赛季时逐个滚动，不会跳过归档；
* 赛季推进只由单进程房间管理器加锁调用，避免同一进程内重复结算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.app.config import Settings
from server.app.db import utcnow_naive
from server.app.glicko2 import season_soft_reset
from server.app.models import Season, SeasonRating, User

logger = logging.getLogger(__name__)

MAX_ROLLOVERS_PER_CALL = 24


def season_name(number: int) -> str:
    return f"第 {number} 赛季"


@dataclass(frozen=True)
class SeasonInfo:
    """对外展示的赛季摘要。"""

    id: str
    number: int
    name: str
    starts_at: datetime
    ends_at: datetime

    def payload(self, now: Optional[datetime] = None) -> dict:
        current = now or utcnow_naive()
        remaining = max(0.0, (self.ends_at - current).total_seconds())
        return {
            "id": self.id,
            "number": self.number,
            "name": self.name,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "days_remaining": int(remaining // 86400),
            # ends_at 是不带时区的 UTC 时间，不能直接用本地时区换算。
            "ends_at_ms": int(
                self.ends_at.replace(tzinfo=timezone.utc).timestamp()
                * 1000
            ),
        }

    @classmethod
    def from_row(cls, row: Season) -> "SeasonInfo":
        return cls(
            id=row.id,
            number=row.number,
            name=row.name,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
        )


async def latest_season(session: AsyncSession) -> Optional[Season]:
    return await session.scalar(
        select(Season).order_by(Season.number.desc()).limit(1)
    )


async def _archive_season(
    session: AsyncSession, season: Season
) -> int:
    """把本赛季参赛账号的最终积分写入归档表；重复调用是幂等的。"""
    users = (
        await session.scalars(
            select(User).where(User.rated_games > 0, User.is_active.is_(True))
        )
    ).all()
    existing = set(
        (
            await session.scalars(
                select(SeasonRating.user_id).where(
                    SeasonRating.season_id == season.id
                )
            )
        ).all()
    )
    archived = 0
    for user in users:
        if user.id in existing:
            continue
        session.add(
            SeasonRating(
                season_id=season.id,
                user_id=user.id,
                rating=user.rating,
                peak_rating=user.peak_rating,
                rating_rd=user.rating_rd,
                rating_volatility=user.rating_volatility,
                rated_games=user.rated_games,
                wins=user.wins,
                losses=user.losses,
                draws=user.draws,
            )
        )
        archived += 1
    return archived


async def _reset_users_for_new_season(session: AsyncSession) -> int:
    users = (
        await session.scalars(
            select(User).where(User.rated_games > 0, User.is_active.is_(True))
        )
    ).all()
    for user in users:
        rating, rd = season_soft_reset(user.rating, user.rating_rd)
        user.rating = rating
        user.rating_rd = rd
        # 跨越赛季的连胜不再连续。
        user.current_win_streak = 0
    return len(users)


async def ensure_current_season(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    now: Optional[datetime] = None,
) -> SeasonInfo:
    """确保存在未结束的赛季；到期则归档并开启下一赛季。"""
    current_time = now or utcnow_naive()
    length = timedelta(days=max(1, settings.season_length_days))
    async with session_factory() as session:
        season = await latest_season(session)
        if season is None:
            season = Season(
                number=1,
                name=season_name(1),
                starts_at=current_time,
                ends_at=current_time + length,
            )
            session.add(season)
            await session.commit()
            await session.refresh(season)
            logger.info(
                "season_created",
                extra={
                    "event": "season_created",
                    "season": season.number,
                },
            )
            return SeasonInfo.from_row(season)

        rollovers = 0
        while current_time >= season.ends_at and rollovers < MAX_ROLLOVERS_PER_CALL:
            archived = await _archive_season(session, season)
            await session.commit()
            reset_count = await _reset_users_for_new_season(session)
            previous = season
            season = Season(
                number=previous.number + 1,
                name=season_name(previous.number + 1),
                starts_at=previous.ends_at,
                ends_at=previous.ends_at + length,
            )
            session.add(season)
            await session.commit()
            await session.refresh(season)
            rollovers += 1
            logger.info(
                "season_rolled",
                extra={
                    "event": "season_rolled",
                    "season": season.number,
                    "archived": archived,
                    "reset_users": reset_count,
                },
            )
        return SeasonInfo.from_row(season)


__all__ = [
    "SeasonInfo",
    "ensure_current_season",
    "latest_season",
    "season_name",
]
