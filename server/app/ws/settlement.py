"""排位积分结算与补结算。

结算的数据库写入与终局写入由调用方控制在同一个事务里提交；只有
提交成功后才把结果落到内存房间，失败时内存状态保持“未结算”。
本模块不导入 ``rooms.py``：赛季来源通过构造函数注入的回调获取。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gomoku.shared.achievements import new_achievement_keys
from server.app.achievement_service import achievement_keys_for_user
from server.app.db import utcnow_naive
from server.app.models import Game, RatingEvent, User
from server.app.ratings import calculate_pair
from server.app.seasons import SeasonInfo
from server.app.ws.entities import Room

logger = logging.getLogger(__name__)


@dataclass
class SettlementResult:
    """一次排位结算的结果。

    结算的数据库写入与终局写入在同一个事务里提交；只有提交成功后
    才把这里的结果落到内存房间，失败时内存状态保持"未结算"。
    """

    black_id: str
    white_id: str
    black_before: int = 0
    black_after: int = 0
    black_delta: int = 0
    white_before: int = 0
    white_after: int = 0
    white_delta: int = 0
    black_achievements: tuple[str, ...] = ()
    white_achievements: tuple[str, ...] = ()
    new_black_achievements: tuple[str, ...] = ()
    new_white_achievements: tuple[str, ...] = ()
    #: True 表示这局此前已经结算过，本次只同步成就，不动积分。
    already_applied: bool = False


class RatingSettlement:
    """排位积分结算器；同一局只应用一次。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        current_season: Callable[[], Awaitable[Optional[SeasonInfo]]],
    ) -> None:
        self._session_factory = session_factory
        self._current_season = current_season
        # 启动补结算只跑一次：网关认证后触发，避免重复扫描整张表。
        self._repair_lock = asyncio.Lock()
        self._repair_done = False

    async def settlement_season_id(self) -> Optional[str]:
        """结算要用的赛季 id。

        ``current_season`` 自己会开一个会话，所以必须在调用方的事务
        开始之前取：把会话嵌套进调用方的事务里既不干净也容易死锁。
        """
        season = await self._current_season()
        return season.id if season is not None else None

    async def settle_rated_game_locked(self, room: Room) -> None:
        """独立事务结算排位积分；同一局只应用一次，好友房直接跳过。

        调用方持有 ``room.lock``。积分、RatingEvent 与
        ``Game.rating_applied`` 在同一个数据库事务中提交，失败则保留
        未结算状态并记录日志。

        需要把"终局状态写入"和"结算"合并进同一个事务时，请改用
        ``settle_rated_game_in_session``（由调用方提供会话）。
        """
        if not room.rated or room.rating_applied:
            return
        season_id = await self.settlement_season_id()
        try:
            async with self._session_factory() as session:
                game_row = await session.get(Game, room.game_id)
                if game_row is None:
                    logger.warning(
                        "排位积分结算缺少对局 room=%s", room.code
                    )
                    return
                result = await self.settle_rated_game_in_session(
                    session,
                    game_row,
                    black_id=room.black_user_id,
                    white_id=room.white_user_id,
                    winner_user_id=room.winner_user_id,
                    status=room.status,
                    result_reason=room.result_reason or "",
                    move_no=room.game.move_count,
                    season_id=season_id,
                )
                if result is None:
                    await session.rollback()
                    return
                await session.commit()
        except Exception:
            logger.exception("排位积分结算失败 room=%s", room.code)
            return
        self.apply_settlement(room, result)

    async def settle_rated_game_in_session(
        self,
        session: AsyncSession,
        game_row: Game,
        *,
        black_id: Optional[str],
        white_id: Optional[str],
        winner_user_id: Optional[str],
        status: str,
        result_reason: str,
        move_no: int,
        season_id: Optional[str],
    ) -> Optional[SettlementResult]:
        """在给定会话里完成排位结算的数据库部分。

        本方法不提交事务、也不修改内存房间：调用方在同一个事务里
        提交之后，才用 ``apply_settlement`` 把结果落到房间里；提交
        失败则整体回滚，内存状态保持"未结算"。

        返回 None 表示这一局不需要（或无法）结算。
        """
        if not black_id or not white_id:
            return None
        if winner_user_id is None:
            score_black = 0.5
        elif winner_user_id == black_id:
            score_black = 1.0
        elif winner_user_id == white_id:
            score_black = 0.0
        else:
            return None

        users = {
            user.id: user
            for user in (
                await session.scalars(
                    select(User).where(
                        User.id.in_((black_id, white_id))
                    )
                )
            ).all()
        }
        black_user = users.get(black_id)
        white_user = users.get(white_id)
        if black_user is None or white_user is None:
            logger.warning("排位积分结算缺少玩家 game=%s", game_row.id)
            return None
        if game_row.rating_applied:
            # 已经结算过：只把成就同步回来，不再动积分。
            return SettlementResult(
                black_id=black_id,
                white_id=white_id,
                black_achievements=achievement_keys_for_user(black_user),
                white_achievements=achievement_keys_for_user(white_user),
                already_applied=True,
            )

        # 结算前先记下成就集合，结算后做差得到"本局新解锁"。
        black_achievements_before = achievement_keys_for_user(black_user)
        white_achievements_before = achievement_keys_for_user(white_user)

        black_change, white_change = calculate_pair(
            black_user.rating,
            black_user.rating_rd,
            black_user.rating_volatility,
            white_user.rating,
            white_user.rating_rd,
            white_user.rating_volatility,
            score_black,
        )
        black_user.rating = black_change.after
        white_user.rating = white_change.after
        black_user.rating_rd = black_change.rd_after
        white_user.rating_rd = white_change.rd_after
        black_user.rating_volatility = black_change.volatility_after
        white_user.rating_volatility = white_change.volatility_after
        black_user.peak_rating = max(
            black_user.peak_rating, black_change.after
        )
        white_user.peak_rating = max(
            white_user.peak_rating, white_change.after
        )
        black_user.rated_games += 1
        white_user.rated_games += 1

        if score_black == 1.0:
            black_user.wins += 1
            white_user.losses += 1
            black_score, white_score = "win", "loss"
            black_user.current_win_streak += 1
            black_user.best_win_streak = max(
                black_user.best_win_streak,
                black_user.current_win_streak,
            )
            white_user.current_win_streak = 0
        elif score_black == 0.0:
            white_user.wins += 1
            black_user.losses += 1
            black_score, white_score = "loss", "win"
            white_user.current_win_streak += 1
            white_user.best_win_streak = max(
                white_user.best_win_streak,
                white_user.current_win_streak,
            )
            black_user.current_win_streak = 0
        else:
            black_user.draws += 1
            white_user.draws += 1
            black_score = white_score = "draw"
            black_user.current_win_streak = 0
            white_user.current_win_streak = 0

        black_achievements_after = achievement_keys_for_user(black_user)
        white_achievements_after = achievement_keys_for_user(white_user)
        new_black_achievements = new_achievement_keys(
            black_achievements_before,
            black_achievements_after,
        )
        new_white_achievements = new_achievement_keys(
            white_achievements_before,
            white_achievements_after,
        )

        session.add_all(
            [
                RatingEvent(
                    game_id=game_row.id,
                    user_id=black_id,
                    opponent_id=white_id,
                    score=black_score,
                    rating_before=black_change.before,
                    rating_after=black_change.after,
                    delta=black_change.delta,
                    rating_rd_before=black_change.rd_before,
                    rating_rd_after=black_change.rd_after,
                    rating_volatility_before=(
                        black_change.volatility_before
                    ),
                    rating_volatility_after=(
                        black_change.volatility_after
                    ),
                    season_id=season_id,
                ),
                RatingEvent(
                    game_id=game_row.id,
                    user_id=white_id,
                    opponent_id=black_id,
                    score=white_score,
                    rating_before=white_change.before,
                    rating_after=white_change.after,
                    delta=white_change.delta,
                    rating_rd_before=white_change.rd_before,
                    rating_rd_after=white_change.rd_after,
                    rating_volatility_before=(
                        white_change.volatility_before
                    ),
                    rating_volatility_after=(
                        white_change.volatility_after
                    ),
                    season_id=season_id,
                ),
            ]
        )
        game_row.rated = True
        game_row.rating_applied = True
        game_row.status = status
        game_row.winner_user_id = winner_user_id
        game_row.result_reason = result_reason or (
            "five" if winner_user_id else "draw"
        )
        game_row.ended_at = game_row.ended_at or utcnow_naive()
        game_row.move_no = move_no
        return SettlementResult(
            black_id=black_id,
            white_id=white_id,
            black_before=black_change.before,
            black_after=black_change.after,
            black_delta=black_change.delta,
            white_before=white_change.before,
            white_after=white_change.after,
            white_delta=white_change.delta,
            black_achievements=black_achievements_after,
            white_achievements=white_achievements_after,
            new_black_achievements=new_black_achievements,
            new_white_achievements=new_white_achievements,
        )

    def apply_settlement(
        self, room: Room, result: SettlementResult
    ) -> None:
        """把已经提交的结算结果落到内存房间。"""
        room.rating_applied = True
        room.black_achievements = result.black_achievements
        room.white_achievements = result.white_achievements
        if result.already_applied:
            return
        room.rating_before = {
            result.black_id: result.black_before,
            result.white_id: result.white_before,
        }
        room.rating_after = {
            result.black_id: result.black_after,
            result.white_id: result.white_after,
        }
        room.rating_delta = {
            result.black_id: result.black_delta,
            result.white_id: result.white_delta,
        }
        room.new_achievements = {
            result.black_id: result.new_black_achievements,
            result.white_id: result.new_white_achievements,
        }

    async def repair_unsettled_games(self) -> int:
        """补结算"已终局但没结算"的排位对局，返回补结算的对局数。

        五连终局与认输/超时/逃跑终局现在都把终局写入与积分结算放在
        同一个事务里提交；但旧版本留下的数据仍可能停在 ``status``
        已终局而 ``rating_applied`` 为假。这类对局不在
        ``mark_unfinished_games_abandoned`` 的扫描范围内，积分就永远
        丢了。服务启动时补一遍。
        """
        try:
            async with self._session_factory() as session:
                game_ids = (
                    await session.scalars(
                        select(Game.id).where(
                            Game.status.in_(
                                ("black_won", "white_won", "draw")
                            ),
                            Game.rated.is_(True),
                            Game.rating_applied.is_(False),
                        )
                    )
                ).all()
        except Exception:
            logger.exception("扫描未结算的排位对局失败")
            return 0
        if not game_ids:
            return 0
        season_id = await self.settlement_season_id()
        repaired = 0
        for game_id in game_ids:
            try:
                async with self._session_factory() as session:
                    game_row = await session.get(Game, game_id)
                    if game_row is None or game_row.rating_applied:
                        continue
                    result = await self.settle_rated_game_in_session(
                        session,
                        game_row,
                        black_id=game_row.black_user_id,
                        white_id=game_row.white_user_id,
                        winner_user_id=game_row.winner_user_id,
                        status=game_row.status,
                        result_reason=game_row.result_reason or "",
                        move_no=game_row.move_no,
                        season_id=season_id,
                    )
                    if result is None:
                        await session.rollback()
                        continue
                    await session.commit()
                    repaired += 1
            except Exception:
                logger.exception(
                    "补结算排位对局失败 game=%s", game_id
                )
        if repaired:
            logger.warning(
                "服务启动：补结算 %s 个未结算的排位对局", repaired
            )
        return repaired

    async def repair_unsettled_games_once(self) -> int:
        """整个进程只补结算一次；重复调用直接返回 0。

        挂在 WebSocket 网关认证之后：真正的启动钩子在 ``main.py`` 里
        （由集成方加一行 ``await room_manager.repair_unsettled_games()``），
        这里作为兜底，保证只要有连接进来就会补一次。
        """
        async with self._repair_lock:
            if self._repair_done:
                return 0
            self._repair_done = True
        return await self.repair_unsettled_games()


__all__ = ["RatingSettlement", "SettlementResult"]
