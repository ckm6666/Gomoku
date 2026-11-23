"""排位赛季信息与历史赛季排行榜。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.avatar import normalize_avatar
from server.app.api.social_helpers import avatar_version_of
from server.app.dependencies import get_current_user, get_db
from server.app.models import Season, SeasonRating, User
from server.app.schemas import (
    SeasonArchiveEntry,
    SeasonArchiveResponse,
    SeasonCurrentResponse,
    SeasonInfoResponse,
)
from server.app.seasons import SeasonInfo, ensure_current_season

router = APIRouter(tags=["seasons"])


async def _current_info(request: Request) -> SeasonInfo:
    manager = getattr(request.app.state, "room_manager", None)
    if manager is not None:
        season = await manager.current_season()
        if season is not None:
            return season
    settings = request.app.state.settings
    return await ensure_current_season(
        request.app.state.session_factory, settings
    )


@router.get("/seasons/current", response_model=SeasonCurrentResponse)
async def current_season(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SeasonCurrentResponse:
    """当前赛季信息与自己的赛季排名。"""
    info = await _current_info(request)
    rank = None
    if current_user.rated_games > 0:
        higher = await session.scalar(
            select(func.count(User.id)).where(
                User.is_active.is_(True),
                User.rated_games > 0,
                User.rating > current_user.rating,
            )
        )
        rank = int(higher or 0) + 1
    return SeasonCurrentResponse(
        season=SeasonInfoResponse(**info.payload()),
        your_rank=rank,
        your_rating=current_user.rating,
        your_rd=round(float(current_user.rating_rd), 1),
        rated_games=current_user.rated_games,
    )


@router.get(
    "/seasons/{number}/leaderboard",
    response_model=SeasonArchiveResponse,
)
async def season_leaderboard(
    number: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    _current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> SeasonArchiveResponse:
    """历史赛季的归档排行榜。"""
    season = await session.scalar(
        select(Season).where(Season.number == number)
    )
    if season is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="赛季不存在"
        )
    conditions = (SeasonRating.season_id == season.id,)
    total = await session.scalar(
        select(func.count(SeasonRating.id)).where(*conditions)
    )
    rows = (
        await session.execute(
            select(SeasonRating, User)
            .join(User, User.id == SeasonRating.user_id)
            .where(*conditions)
            .order_by(
                SeasonRating.rating.desc(),
                SeasonRating.rated_games.desc(),
                User.username_key.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    entries = [
        SeasonArchiveEntry(
            position=offset + index + 1,
            user_id=user.id,
            username=user.username,
            avatar=normalize_avatar(user.avatar, user.id),
            avatar_version=avatar_version_of(user),
            rating=rating.rating,
            peak_rating=rating.peak_rating,
            rated_games=rating.rated_games,
            wins=rating.wins,
            losses=rating.losses,
            draws=rating.draws,
        )
        for index, (rating, user) in enumerate(rows)
    ]
    info = SeasonInfo(
        id=season.id,
        number=season.number,
        name=season.name,
        starts_at=season.starts_at,
        ends_at=season.ends_at,
    )
    return SeasonArchiveResponse(
        season=SeasonInfoResponse(**info.payload()),
        entries=entries,
        total=int(total or 0),
    )


__all__ = ["router"]
