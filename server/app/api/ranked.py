"""排位资料、最近战绩与排行榜接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gomoku.shared.avatar import normalize_avatar
from server.app.api.social_helpers import avatar_version_of
from server.app.dependencies import get_current_user, get_db
from server.app.models import RatingEvent, User
from server.app.ratings import rank_for_rating
from server.app.schemas import (
    LeaderboardEntry,
    LeaderboardResponse,
    ProfileResponse,
    RecentRatingGame,
    UserResponse,
)

router = APIRouter(tags=["ranked"])


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def leaderboard(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    _current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> LeaderboardResponse:
    """按积分排序的排行榜；只包含完成过至少一局排位的活跃账号。"""
    conditions = (User.rated_games > 0, User.is_active.is_(True))
    total = await session.scalar(
        select(func.count(User.id)).where(*conditions)
    )
    rows = (
        await session.scalars(
            select(User)
            .where(*conditions)
            .order_by(
                User.rating.desc(),
                User.rated_games.desc(),
                User.username_key.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()

    entries = [
        LeaderboardEntry(
            position=offset + index + 1,
            user_id=user.id,
            username=user.username,
            avatar=normalize_avatar(user.avatar, user.id),
            avatar_version=avatar_version_of(user),
            rating=user.rating,
            rank_name=rank_for_rating(user.rating).name,
            rated_games=user.rated_games,
            wins=user.wins,
            losses=user.losses,
            draws=user.draws,
        )
        for index, user in enumerate(rows)
    ]
    return LeaderboardResponse(entries=entries, total=int(total or 0))


@router.get("/profile/me", response_model=ProfileResponse)
async def profile_me(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    """当前账号的积分资料与最近 10 场排位战绩。"""
    events = (
        await session.scalars(
            select(RatingEvent)
            .where(RatingEvent.user_id == current_user.id)
            .order_by(RatingEvent.created_at.desc())
            .limit(10)
        )
    ).all()

    opponent_names: dict[str, str] = {}
    opponent_ids = {event.opponent_id for event in events}
    if opponent_ids:
        rows = await session.execute(
            select(User.id, User.username).where(User.id.in_(opponent_ids))
        )
        opponent_names = {user_id: name for user_id, name in rows.all()}

    recent_games = [
        RecentRatingGame(
            game_id=event.game_id,
            opponent_username=opponent_names.get(
                event.opponent_id, "已注销玩家"
            ),
            score=event.score,
            rating_before=event.rating_before,
            rating_after=event.rating_after,
            delta=event.delta,
            created_at=event.created_at,
        )
        for event in events
    ]
    return ProfileResponse(
        user=UserResponse.model_validate(current_user),
        recent_games=recent_games,
    )


__all__ = ["router"]
