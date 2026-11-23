"""排位积分（Glicko-2）与休闲段位。

设计要点：
* 采用 Glicko-2 同时维护积分、评分不确定度（RD）和波动率；
* 新账号起始 1200 分（黄金段位），最低 100 分；
* 排位对局由服务器结算一次，积分/RD/波动率变化写入 rating_events 审计；
* 好友房不计分，避免通过固定对手刷分；
* 赛季切换时调用 :func:`server.app.glicko2.season_soft_reset` 做软重置；
* 段位阈值只存在服务端，客户端通过接口/对局快照读取，避免两端规则漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from server.app.glicko2 import (
    GlickoState,
    calculate_pair as _calculate_glicko_pair,
)

START_RATING = 1200
MIN_RATING = 100


# 段位从低到高；next threshold 由下一档自动推导。
_RANK_TIERS: tuple[tuple[str, str, int], ...] = (
    ("bronze", "青铜", 0),
    ("silver", "白银", 1100),
    ("gold", "黄金", 1200),
    ("platinum", "铂金", 1350),
    ("diamond", "钻石", 1500),
    ("master", "大师", 1650),
    ("legend", "王者", 1800),
)


@dataclass(frozen=True)
class RankTier:
    """一个段位区间。"""

    key: str
    name: str
    min_rating: int
    next_rating: Optional[int]


def _build_tiers() -> tuple[RankTier, ...]:
    tiers: list[RankTier] = []
    for index, (key, name, minimum) in enumerate(_RANK_TIERS):
        next_rating = (
            _RANK_TIERS[index + 1][2]
            if index + 1 < len(_RANK_TIERS)
            else None
        )
        tiers.append(RankTier(key, name, minimum, next_rating))
    return tuple(tiers)


RANK_TIERS: tuple[RankTier, ...] = _build_tiers()


def rank_for_rating(rating: int) -> RankTier:
    """返回积分对应段位；非法/过低积分回退到青铜。"""
    for tier in reversed(RANK_TIERS):
        if rating >= tier.min_rating:
            return tier
    return RANK_TIERS[0]


def rank_progress_percent(rating: int) -> int:
    """当前段位内的进度百分比；王者固定 100。"""
    tier = rank_for_rating(rating)
    if tier.next_rating is None:
        return 100
    span = max(1, tier.next_rating - tier.min_rating)
    progress = (rating - tier.min_rating) * 100 / span
    return max(0, min(100, int(progress)))


def rank_payload(rating: int) -> dict:
    """给 HTTP/WS 响应使用的段位摘要。"""
    tier = rank_for_rating(rating)
    return {
        "key": tier.key,
        "name": tier.name,
        "next_rating": tier.next_rating,
        "progress": rank_progress_percent(rating),
    }


@dataclass(frozen=True)
class RatingChange:
    """一名玩家一局的积分变化（含 Glicko-2 审计字段）。"""

    before: int
    after: int
    rd_before: float
    rd_after: float
    volatility_before: float
    volatility_after: float

    @property
    def delta(self) -> int:
        return self.after - self.before


def calculate_pair(
    rating_a: int,
    rd_a: float,
    volatility_a: float,
    rating_b: int,
    rd_b: float,
    volatility_b: float,
    score_a: float,
) -> tuple[RatingChange, RatingChange]:
    """结算一局排位赛：``score_a`` 为 A 的得分（胜 1 / 平 0.5 / 负 0）。

    返回顺序与传入顺序一致。积分最低钳制到 ``MIN_RATING``。
    """
    glicko_a, glicko_b = _calculate_glicko_pair(
        GlickoState.from_stored(rating_a, rd_a, volatility_a),
        GlickoState.from_stored(rating_b, rd_b, volatility_b),
        score_a,
    )
    return (
        RatingChange(
            before=rating_a,
            after=max(MIN_RATING, int(round(glicko_a.after.rating))),
            rd_before=glicko_a.before.rd,
            rd_after=glicko_a.after.rd,
            volatility_before=glicko_a.before.volatility,
            volatility_after=glicko_a.after.volatility,
        ),
        RatingChange(
            before=rating_b,
            after=max(MIN_RATING, int(round(glicko_b.after.rating))),
            rd_before=glicko_b.before.rd,
            rd_after=glicko_b.after.rd,
            volatility_before=glicko_b.before.volatility,
            volatility_after=glicko_b.after.volatility,
        ),
    )


__all__ = [
    "START_RATING",
    "MIN_RATING",
    "RANK_TIERS",
    "RankTier",
    "rank_for_rating",
    "rank_progress_percent",
    "rank_payload",
    "RatingChange",
    "calculate_pair",
]
