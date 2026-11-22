"""联机对局历史与棋谱回放接口。

只允许对局参与者读取；对外不暴露房间码。列表只返回已结束对局，详情允许
读取进行中/已作废对局，便于客户端和后续审计复用。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.dependencies import get_current_user, get_db
from server.app.models import Game, Move as MoveRow, RatingEvent, User
from server.app.schemas import (
    GameDetailResponse,
    GameMove,
    LiveGameEntry,
    LiveGamesResponse,
    RecentGameEntry,
    RecentGamesResponse,
)

router = APIRouter(tags=["games"])

_COMPLETED_STATUSES = ("black_won", "white_won", "draw")


async def _usernames(
    session: AsyncSession, user_ids: set[str]
) -> dict[str, str]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(User.id, User.username).where(User.id.in_(user_ids))
    )
    return {user_id: username for user_id, username in rows.all()}


def _your_color(game: Game, user_id: str) -> str:
    return "black" if game.black_user_id == user_id else "white"


def _opponent_id(game: Game, user_id: str) -> str | None:
    if game.black_user_id == user_id:
        return game.white_user_id
    return game.black_user_id


def _result_for(game: Game, user_id: str) -> str:
    if game.winner_user_id is None:
        return "draw"
    return "win" if game.winner_user_id == user_id else "loss"


@router.get("/games/recent", response_model=RecentGamesResponse)
async def recent_games(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> RecentGamesResponse:
    """当前用户参与过的已结束对局，按结束时间倒序。"""
    conditions = (
        or_(
            Game.black_user_id == current_user.id,
            Game.white_user_id == current_user.id,
        ),
        Game.status.in_(_COMPLETED_STATUSES),
    )
    total = await session.scalar(
        select(func.count(Game.id)).where(*conditions)
    )
    games = (
        await session.scalars(
            select(Game)
            .where(*conditions)
            .order_by(
                func.coalesce(Game.ended_at, Game.created_at).desc()
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()

    opponent_ids = {
        opponent
        for game in games
        if (opponent := _opponent_id(game, current_user.id)) is not None
    }
    names = await _usernames(session, opponent_ids)

    deltas: dict[str, int] = {}
    game_ids = [game.id for game in games]
    if game_ids:
        rows = await session.execute(
            select(RatingEvent.game_id, RatingEvent.delta).where(
                RatingEvent.user_id == current_user.id,
                RatingEvent.game_id.in_(game_ids),
            )
        )
        deltas = {game_id: delta for game_id, delta in rows.all()}

    entries = []
    for game in games:
        opponent_id = _opponent_id(game, current_user.id)
        entries.append(
            RecentGameEntry(
                game_id=game.id,
                rated=game.rated,
                opponent_username=(
                    names.get(opponent_id, "对手")
                    if opponent_id is not None
                    else "对手"
                ),
                your_color=_your_color(game, current_user.id),
                result=_result_for(game, current_user.id),
                result_reason=game.result_reason,
                move_no=game.move_no,
                ended_at=game.ended_at,
                rating_delta=deltas.get(game.id),
            )
        )

    return RecentGamesResponse(entries=entries, total=int(total or 0))


@router.get("/games/live", response_model=LiveGamesResponse)
async def live_games(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> LiveGamesResponse:
    """当前正在进行中的对局列表，供观战入口挑选。

    路由必须排在 ``/games/{game_id}`` 之前，否则 "live" 会被当成
    对局编号。房间表只存在于应用生命周期内，所以先取属性再判断。
    """
    room_manager = getattr(request.app.state, "room_manager", None)
    entries: list[LiveGameEntry] = []
    if room_manager is not None:
        entries = [
            LiveGameEntry(**entry) for entry in room_manager.list_live_games()
        ]
    return LiveGamesResponse(entries=entries, total=len(entries))


@router.get("/games/{game_id}", response_model=GameDetailResponse)
async def game_detail(
    game_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> GameDetailResponse:
    """返回参与者可见的完整对局与落子序列。"""
    game = await session.get(Game, game_id)
    if game is None or current_user.id not in (
        game.black_user_id,
        game.white_user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="对局不存在",
        )

    move_rows = (
        await session.scalars(
            select(MoveRow)
            .where(MoveRow.game_id == game_id)
            .order_by(MoveRow.move_no.asc())
        )
    ).all()

    participant_ids = {
        user_id
        for user_id in (game.black_user_id, game.white_user_id)
        if user_id
    }
    names = await _usernames(session, participant_ids)
    rating_delta = await session.scalar(
        select(RatingEvent.delta).where(
            RatingEvent.game_id == game_id,
            RatingEvent.user_id == current_user.id,
        )
    )

    return GameDetailResponse(
        game_id=game.id,
        rated=game.rated,
        status=game.status,
        your_color=_your_color(game, current_user.id),
        black_user_id=game.black_user_id,
        black_username=names.get(game.black_user_id, "已注销玩家"),
        white_user_id=game.white_user_id,
        white_username=(
            names.get(game.white_user_id, "已注销玩家")
            if game.white_user_id
            else ""
        ),
        winner_user_id=game.winner_user_id,
        result_reason=game.result_reason,
        move_no=game.move_no,
        created_at=game.created_at,
        started_at=game.started_at,
        ended_at=game.ended_at,
        rating_delta=rating_delta,
        moves=[
            GameMove(
                move_no=move.move_no,
                row=move.row,
                col=move.col,
                color=move.color,
                created_at=move.created_at,
            )
            for move in move_rows
        ],
    )


__all__ = ["router"]
