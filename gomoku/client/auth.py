"""客户端认证会话数据模型（不包含任何密码/明文 token 日志）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from gomoku.shared.achievements import (
    AchievementStats,
    normalize_achievement_keys,
    unlocked_achievement_keys,
)
from gomoku.shared.avatar import DEFAULT_AVATAR, normalize_avatar
from gomoku.shared.errors import ProtocolError


def _str_field(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"认证响应缺少 {key}")
    return value


def _optional_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _optional_int(
    data: Mapping[str, Any], key: str, default: int
) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return value


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str
    avatar: str = DEFAULT_AVATAR
    avatar_version: int = 0
    # Stage 2：服务器认证响应携带的排位资料；旧服务器缺省时客户端仍可登录。
    rating: int = 1200
    peak_rating: int = 1200
    rated_games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    rank_name: str = ""
    rank_key: str = ""
    next_rank_rating: Optional[int] = None
    rank_progress: int = 0
    win_rate: float = 0.0
    # 成就与连胜统计（旧服务器缺省为空，客户端按统计推导）。
    current_win_streak: int = 0
    best_win_streak: int = 0
    achievements: tuple[str, ...] = ()

    @property
    def achievement_keys(self) -> tuple[str, ...]:
        """优先使用服务器下发的成就；否则按统计本地推导。"""
        if self.achievements:
            return self.achievements
        return unlocked_achievement_keys(
            AchievementStats(
                wins=self.wins,
                rated_games=self.rated_games,
                current_streak=self.current_win_streak,
                best_streak=self.best_win_streak,
                rating=self.rating,
                peak_rating=self.peak_rating,
            )
        )


@dataclass(frozen=True)
class AuthSession:
    """内存中的登录会话。

    Stage 1 不把 token 持久化到明文文件；关闭客户端后需要重新登录。
    后续可通过 Windows DPAPI 安全保存 refresh token。
    """

    access_token: str
    refresh_token: str
    user: AuthUser
    expires_in: int
    obtained_at: float = field(default_factory=time.monotonic)

    @property
    def access_expires_at(self) -> float:
        return self.obtained_at + self.expires_in

    def is_access_expired(self, margin_seconds: float = 5.0) -> bool:
        return time.monotonic() >= self.access_expires_at - margin_seconds

    @classmethod
    def from_response(cls, data: Mapping[str, Any]) -> "AuthSession":
        access = _str_field(data, "access_token")
        refresh = _str_field(data, "refresh_token")
        raw_user = data.get("user")
        if not isinstance(raw_user, Mapping):
            raise ProtocolError("认证响应缺少 user")
        next_rank = raw_user.get("next_rank_rating")
        if not isinstance(next_rank, int) or isinstance(next_rank, bool):
            next_rank = None
        raw_win_rate = raw_user.get("win_rate", 0.0)
        win_rate = (
            float(raw_win_rate)
            if isinstance(raw_win_rate, (int, float))
            and not isinstance(raw_win_rate, bool)
            else 0.0
        )
        user_id = _str_field(raw_user, "id")
        user = AuthUser(
            id=user_id,
            username=_str_field(raw_user, "username"),
            avatar=normalize_avatar(raw_user.get("avatar"), user_id),
            avatar_version=_optional_int(raw_user, "avatar_version", 0),
            rating=_optional_int(raw_user, "rating", 1200),
            peak_rating=_optional_int(raw_user, "peak_rating", 1200),
            rated_games=_optional_int(raw_user, "rated_games", 0),
            wins=_optional_int(raw_user, "wins", 0),
            losses=_optional_int(raw_user, "losses", 0),
            draws=_optional_int(raw_user, "draws", 0),
            rank_name=_optional_str(raw_user, "rank_name"),
            rank_key=_optional_str(raw_user, "rank_key"),
            next_rank_rating=next_rank,
            rank_progress=_optional_int(raw_user, "rank_progress", 0),
            win_rate=win_rate,
            current_win_streak=_optional_int(
                raw_user, "current_win_streak", 0
            ),
            best_win_streak=_optional_int(
                raw_user, "best_win_streak", 0
            ),
            achievements=normalize_achievement_keys(
                raw_user.get("achievements")
            ),
        )
        expires_in = data.get("expires_in", 900)
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise ProtocolError("认证响应 expires_in 非法")
        return cls(
            access_token=access,
            refresh_token=refresh,
            user=user,
            expires_in=expires_in,
        )


__all__ = ["AuthUser", "AuthSession"]
