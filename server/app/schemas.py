"""认证 HTTP API 的 Pydantic 请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
)

from server.app.ratings import (
    START_RATING,
    rank_for_rating,
    rank_progress_percent,
)
from server.app.security import validate_password, validate_username


class RegisterRequest(BaseModel):
    username: Annotated[str, Field(min_length=2, max_length=24)]
    password: Annotated[str, Field(min_length=8, max_length=128)]
    invite_code: Annotated[str, Field(min_length=4, max_length=64)]

    @field_validator("username")
    @classmethod
    def _username_valid(cls, value: str) -> str:
        if not validate_username(value):
            raise ValueError("用户名只能包含字母、数字、中文或下划线，长度 2～24")
        return value.strip()

    @field_validator("password")
    @classmethod
    def _password_valid(cls, value: str) -> str:
        if not validate_password(value):
            raise ValueError("密码长度必须为 8～128 个字符")
        return value

    @field_validator("invite_code")
    @classmethod
    def _invite_valid(cls, value: str) -> str:
        return value.strip().upper()


class LoginRequest(BaseModel):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(BaseModel):
    refresh_token: Annotated[str, Field(min_length=16, max_length=256)]


class LogoutRequest(BaseModel):
    refresh_token: Annotated[str, Field(min_length=16, max_length=256)]


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    created_at: datetime
    rating: int = START_RATING
    peak_rating: int = START_RATING
    rated_games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0

    @computed_field
    @property
    def rank_name(self) -> str:
        return rank_for_rating(self.rating).name

    @computed_field
    @property
    def rank_key(self) -> str:
        return rank_for_rating(self.rating).key

    @computed_field
    @property
    def next_rank_rating(self) -> Optional[int]:
        return rank_for_rating(self.rating).next_rating

    @computed_field
    @property
    def rank_progress(self) -> int:
        return rank_progress_percent(self.rating)

    @computed_field
    @property
    def win_rate(self) -> float:
        if self.rated_games <= 0:
            return 0.0
        return round(self.wins * 100.0 / self.rated_games, 1)


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class LeaderboardEntry(BaseModel):
    position: int
    user_id: str
    username: str
    rating: int
    rank_name: str
    rated_games: int
    wins: int
    losses: int
    draws: int


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry]
    total: int


class RecentRatingGame(BaseModel):
    game_id: str
    opponent_username: str
    score: str
    rating_before: int
    rating_after: int
    delta: int
    created_at: datetime


class ProfileResponse(BaseModel):
    user: UserResponse
    recent_games: list[RecentRatingGame]


class GameMove(BaseModel):
    move_no: int
    row: int
    col: int
    color: str
    created_at: datetime


class RecentGameEntry(BaseModel):
    game_id: str
    rated: bool
    opponent_username: str
    your_color: str
    result: str  # win/loss/draw
    result_reason: Optional[str] = None
    move_no: int
    ended_at: Optional[datetime] = None
    rating_delta: Optional[int] = None


class RecentGamesResponse(BaseModel):
    entries: list[RecentGameEntry]
    total: int


class GameDetailResponse(BaseModel):
    game_id: str
    rated: bool
    status: str
    your_color: str
    black_user_id: str
    black_username: str
    white_user_id: Optional[str] = None
    white_username: str
    winner_user_id: Optional[str] = None
    result_reason: Optional[str] = None
    move_no: int
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    rating_delta: Optional[int] = None
    moves: list[GameMove]


__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "RefreshRequest",
    "LogoutRequest",
    "UserResponse",
    "AuthResponse",
    "LeaderboardEntry",
    "LeaderboardResponse",
    "RecentRatingGame",
    "ProfileResponse",
    "GameMove",
    "RecentGameEntry",
    "RecentGamesResponse",
    "GameDetailResponse",
]
