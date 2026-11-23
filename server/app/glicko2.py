"""Glicko-2 积分算法（纯函数，方便单元测试）。

实现遵循 Mark Glickman 的 Glicko-2 论文：
* 每局视为一个评分周期，只包含一条对局结果；
* 评分内部使用 ``(mu, phi, sigma)``，对外换算成整数积分；
* 量表中心取 1200，与项目原有段位阈值保持一致；
* RD 限制在 30～350，避免长时间不下的账号积分几乎不动或剧烈波动。

服务端在结算排位对局时调用 :func:`calculate_pair`，并把 RD/波动率变化
写入 ``rating_events`` 便于审计。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

GLICKO_SCALE = 173.7178
RATING_CENTER = 1200.0
START_RD = 100.0
MIN_RD = 30.0
MAX_RD = 350.0
START_VOLATILITY = 0.06
DEFAULT_TAU = 0.5
#: 赛季软重置后的最低 RD；让回归玩家更快回到真实水平。
SEASON_RESET_RD = 120.0
_EPSILON = 0.000001


@dataclass(frozen=True)
class GlickoState:
    """一名玩家的 Glicko-2 状态。"""

    rating: float
    rd: float
    volatility: float

    @classmethod
    def from_stored(
        cls, rating: int | float, rd: float, volatility: float
    ) -> "GlickoState":
        return cls(
            rating=float(rating),
            rd=_clamp_rd(float(rd)),
            volatility=max(0.001, float(volatility)),
        )


@dataclass(frozen=True)
class GlickoChange:
    """一名玩家一局的 Glicko-2 状态变化。"""

    before: GlickoState
    after: GlickoState


def _clamp_rd(rd: float) -> float:
    return max(MIN_RD, min(MAX_RD, rd))


def _to_mu_phi(state: GlickoState) -> tuple[float, float]:
    return (
        (state.rating - RATING_CENTER) / GLICKO_SCALE,
        _clamp_rd(state.rd) / GLICKO_SCALE,
    )


def _from_mu_phi(
    mu: float, phi: float, volatility: float
) -> GlickoState:
    return GlickoState(
        rating=RATING_CENTER + GLICKO_SCALE * mu,
        rd=_clamp_rd(GLICKO_SCALE * phi),
        volatility=volatility,
    )


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expected(mu: float, mu_opponent: float, g: float) -> float:
    return 1.0 / (1.0 + math.exp(-g * (mu - mu_opponent)))


def _new_volatility(
    phi: float,
    v: float,
    delta: float,
    volatility: float,
    tau: float,
) -> float:
    """用 Illinois 算法求新的波动率。"""
    # 注意：论文里的 ``a`` 是常量 ln(sigma^2)，不能与求根区间的端点
    # 变量混用；f(x) 的闭包必须捕获这个常量。
    log_sigma2 = math.log(volatility * volatility)

    def f(x: float) -> float:
        exp_x = math.exp(x)
        numerator = exp_x * (
            delta * delta - phi * phi - v - exp_x
        )
        denominator = 2.0 * (phi * phi + v + exp_x) ** 2
        return (
            numerator / denominator
            - (x - log_sigma2) / (tau * tau)
        )

    a = log_sigma2
    if delta * delta > phi * phi + v:
        b = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0.0:
            k += 1
            if k > 1000:  # pragma: no cover - 数值兜底
                return volatility
        b = a - k * tau

    f_a = f(a)
    f_b = f(b)
    iterations = 0
    while abs(b - a) > _EPSILON and iterations < 1000:
        iterations += 1
        c = a + (a - b) * f_a / (f_b - f_a)
        f_c = f(c)
        if f_c * f_b <= 0.0:
            a = b
            f_a = f_b
        else:
            f_a /= 2.0
        b = c
        f_b = f_c
    return math.exp(a / 2.0)


def _update_one(
    state: GlickoState,
    opponent: GlickoState,
    score: float,
    tau: float,
) -> GlickoState:
    mu, phi = _to_mu_phi(state)
    mu_opponent, phi_opponent = _to_mu_phi(opponent)
    g = _g(phi_opponent)
    expected = _expected(mu, mu_opponent, g)
    variance = 1.0 / (g * g * expected * (1.0 - expected))
    delta = variance * g * (score - expected)
    volatility = _new_volatility(
        phi, variance, delta, state.volatility, tau
    )
    phi_star = math.sqrt(phi * phi + volatility * volatility)
    phi_new = 1.0 / math.sqrt(
        1.0 / (phi_star * phi_star) + 1.0 / variance
    )
    mu_new = mu + phi_new * phi_new * g * (score - expected)
    return _from_mu_phi(mu_new, phi_new, volatility)


def calculate_pair(
    black: GlickoState,
    white: GlickoState,
    score_black: float,
    *,
    tau: float = DEFAULT_TAU,
) -> tuple[GlickoChange, GlickoChange]:
    """结算一局：``score_black`` 为黑方得分（胜 1 / 平 0.5 / 负 0）。"""
    if score_black not in (0.0, 0.5, 1.0):
        raise ValueError("score_black 只能是 0.0/0.5/1.0")
    black_after = _update_one(black, white, score_black, tau)
    white_after = _update_one(white, black, 1.0 - score_black, tau)
    return (
        GlickoChange(before=black, after=black_after),
        GlickoChange(before=white, after=white_after),
    )


def rating_after_score(
    rating: int,
    rd: float,
    volatility: float,
    opponent_rating: int,
    opponent_rd: float,
    opponent_volatility: float,
    score: float,
) -> int:
    """便捷函数：只返回结算后的整数积分。"""
    change, _ = calculate_pair(
        GlickoState.from_stored(rating, rd, volatility),
        GlickoState.from_stored(
            opponent_rating, opponent_rd, opponent_volatility
        ),
        score,
    )
    return int(round(change.after.rating))


def season_soft_reset(rating: int, rd: float) -> tuple[int, float]:
    """赛季切换时的积分软重置。

    保留一半的积分偏离量，让高段玩家仍有优势，但新赛季需要重新证明；
    RD 抬高到至少 120，避免老账号在赛季初几乎不动。
    """
    reset_rating = int(round(RATING_CENTER + (float(rating) - RATING_CENTER) * 0.5))
    reset_rd = min(MAX_RD, max(SEASON_RESET_RD, float(rd) * 1.5))
    return reset_rating, reset_rd


__all__ = [
    "DEFAULT_TAU",
    "GLICKO_SCALE",
    "GlickoChange",
    "GlickoState",
    "MAX_RD",
    "MIN_RD",
    "RATING_CENTER",
    "SEASON_RESET_RD",
    "START_RD",
    "START_VOLATILITY",
    "calculate_pair",
    "rating_after_score",
    "season_soft_reset",
]
