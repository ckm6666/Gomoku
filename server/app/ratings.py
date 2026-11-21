"""Elo 积分与休闲段位（纯函数，方便单元测试）。

设计要点：
* 新账号起始 1200 分（黄金段位），最低 100 分；
* 前 10 局为定级保护期，K=40，之后按分段 K=32/24/16；
* 排位对局由服务器结算一次，积分变化写入 rating_events 便于审计；
* 好友房不计分，避免通过固定对手刷分；
* 段位阈值只存在服务端，客户端通过接口/对局快照读取，避免两端规则漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

START_RATING = 1200
MIN_RATING = 100
PROVISIONAL_GAMES = 10

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


def expected_score(rating: int, opponent_rating: int) -> float:
    """Elo 期望胜率（胜=1，平=0.5）。"""
    return 1.0 / (1.0 + 10.0 ** ((opponent_rating - rating) / 400.0))


def k_factor(rating: int, rated_games: int) -> int:
    """按定级期/分段选择 K 值。"""
    if rated_games < PROVISIONAL_GAMES:
        return 40
    if rating < 1600:
        return 32
    if rating < 2000:
        return 24
    return 16


@dataclass(frozen=True)
class RatingChange:
    """一名玩家一局的积分变化。"""

    before: int
    after: int

    @property
    def delta(self) -> int:
        return self.after - self.before


def calculate_pair(
    rating_a: int,
    games_a: int,
    rating_b: int,
    games_b: int,
    score_a: float,
) -> tuple[RatingChange, RatingChange]:
    """计算两名玩家的新积分。

    ``score_a`` 为 A 的得分：胜 1.0、平 0.5、负 0.0；B 得分为其补数。
    """
    if score_a not in (0.0, 0.5, 1.0):
        raise ValueError("score_a 只能是 0.0/0.5/1.0")

    expected_a = expected_score(rating_a, rating_b)
    expected_b = 1.0 - expected_a
    score_b = 1.0 - score_a

    k_a = k_factor(rating_a, games_a)
    k_b = k_factor(rating_b, games_b)

    after_a = max(MIN_RATING, int(round(rating_a + k_a * (score_a - expected_a))))
    after_b = max(MIN_RATING, int(round(rating_b + k_b * (score_b - expected_b))))
    return (
        RatingChange(before=rating_a, after=after_a),
        RatingChange(before=rating_b, after=after_b),
    )


__all__ = [
    "START_RATING",
    "MIN_RATING",
    "PROVISIONAL_GAMES",
    "RANK_TIERS",
    "RankTier",
    "rank_for_rating",
    "rank_progress_percent",
    "rank_payload",
    "expected_score",
    "k_factor",
    "RatingChange",
    "calculate_pair",
]
