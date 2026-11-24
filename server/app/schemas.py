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

from gomoku.shared.achievements import (
    AchievementStats,
    unlocked_achievement_keys,
)
from gomoku.shared.avatar import (
    AVATAR_IMAGE_MAX_BYTES,
    DEFAULT_AVATAR,
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
    avatar: str = DEFAULT_AVATAR
    # 对外只暴露“有自定义图片时才有效”的版本号；模型上的
    # public_avatar_version 属性会把没有图片的情况归一为 0。
    avatar_version: int = Field(
        default=0,
        validation_alias="public_avatar_version",
        serialization_alias="avatar_version",
    )
    created_at: datetime
    rating: int = START_RATING
    peak_rating: int = START_RATING
    rating_rd: float = 100.0
    rated_games: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    current_win_streak: int = 0
    best_win_streak: int = 0

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

    @computed_field
    @property
    def achievements(self) -> list[str]:
        """当前已解锁的成就 key（按成就目录顺序）。"""
        stats = AchievementStats(
            wins=self.wins,
            rated_games=self.rated_games,
            current_streak=self.current_win_streak,
            best_streak=self.best_win_streak,
            rating=self.rating,
            peak_rating=self.peak_rating,
        )
        return list(unlocked_achievement_keys(stats))


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
    avatar: str = DEFAULT_AVATAR
    avatar_version: int = 0
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


class SeasonInfoResponse(BaseModel):
    id: str
    number: int
    name: str
    starts_at: str
    ends_at: str
    days_remaining: int
    ends_at_ms: int


class SeasonCurrentResponse(BaseModel):
    season: SeasonInfoResponse
    your_rank: Optional[int] = None
    your_rating: Optional[int] = None
    your_rd: Optional[float] = None
    rated_games: int = 0


class SeasonArchiveEntry(BaseModel):
    position: int
    user_id: str
    username: str
    avatar: str = DEFAULT_AVATAR
    avatar_version: int = 0
    rating: int
    peak_rating: int
    rated_games: int
    wins: int
    losses: int
    draws: int


class SeasonArchiveResponse(BaseModel):
    season: SeasonInfoResponse
    entries: list[SeasonArchiveEntry]
    total: int


class ProfileResponse(BaseModel):
    user: UserResponse
    recent_games: list[RecentRatingGame]
    season: Optional[SeasonInfoResponse] = None
    season_rank: Optional[int] = None


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
    rule_mode: str = "freestyle"


class RecentGamesResponse(BaseModel):
    entries: list[RecentGameEntry]
    total: int


class LiveGameEntry(BaseModel):
    """正在进行的对局摘要，供观战入口展示。"""

    game_id: str
    rated: bool
    black_username: str
    white_username: str
    black_user_id: str
    white_user_id: str
    move_no: int
    status: str
    rule_mode: str = "freestyle"


class LiveGamesResponse(BaseModel):
    entries: list[LiveGameEntry]
    total: int


class AvatarUpdateRequest(BaseModel):
    avatar: Annotated[str, Field(min_length=1, max_length=16)]


class AvatarImageRequest(BaseModel):
    """上传自定义头像；image 为 ``data:image/jpeg;base64,...``。"""

    image: Annotated[
        str,
        Field(
            min_length=32,
            # 留出 base64 与 data URL 前缀的余量。
            max_length=AVATAR_IMAGE_MAX_BYTES * 2,
        ),
    ]


class PublicProfileResponse(BaseModel):
    user: UserResponse
    online: bool = False
    friendship: str = "none"  # none/pending_out/pending_in/friends/self


class UserSearchEntry(BaseModel):
    id: str
    username: str
    avatar: str = DEFAULT_AVATAR
    avatar_version: int = 0
    rating: int = START_RATING
    rank_name: str
    online: bool = False


class UserSearchResponse(BaseModel):
    entries: list[UserSearchEntry]
    total: int


class FriendEntry(BaseModel):
    user_id: str
    username: str
    avatar: str = DEFAULT_AVATAR
    avatar_version: int = 0
    rating: int = START_RATING
    rank_name: str
    online: bool = False
    unread: int = 0


class FriendRequestEntry(BaseModel):
    request_id: str
    user_id: str
    username: str
    avatar: str = DEFAULT_AVATAR
    avatar_version: int = 0
    rating: int = START_RATING
    rank_name: str
    created_at: datetime


class FriendsResponse(BaseModel):
    friends: list[FriendEntry]
    incoming: list[FriendRequestEntry]
    outgoing: list[FriendRequestEntry]


class FriendRequestCreate(BaseModel):
    user_id: Annotated[str, Field(min_length=1, max_length=36)]


class DirectMessageEntry(BaseModel):
    id: str
    sender_id: str
    sender_name: str
    sender_avatar: str = DEFAULT_AVATAR
    sender_avatar_version: int = 0
    recipient_id: str
    content: str
    created_at: datetime
    sent_at_ms: int


class DirectMessagesResponse(BaseModel):
    messages: list[DirectMessageEntry]


class SendDirectMessageRequest(BaseModel):
    content: Annotated[str, Field(min_length=1, max_length=200)]


class MarkMessagesReadRequest(BaseModel):
    """客户端请求把若干条消息标记为已读。

    只接受消息 id，不接受“全部已读”：客户端只把**真正显示过**的消息
    报上来，没看过的那些必须继续留在未读计数里。
    """

    message_ids: Annotated[list[str], Field(max_length=200)] = Field(
        default_factory=list
    )


class MarkMessagesReadResponse(BaseModel):
    read_count: int = 0


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
    time_control: dict = Field(default_factory=dict)
    rule_mode: str = "freestyle"


class ReportCreateRequest(BaseModel):
    game_id: Annotated[str, Field(min_length=1, max_length=36)]
    category: Annotated[str, Field(min_length=1, max_length=24)]
    description: Annotated[str, Field(default="", max_length=500)] = ""

    @field_validator("category")
    @classmethod
    def _category_valid(cls, value: str) -> str:
        allowed = {"cheating", "abuse", "stalling", "other"}
        if value not in allowed:
            raise ValueError("举报类别必须是 cheating/abuse/stalling/other")
        return value


class ReportEntry(BaseModel):
    id: str
    game_id: str
    reporter_id: str
    reported_user_id: str
    category: str
    description: str
    status: str
    created_at: datetime
    reviewed_at: Optional[datetime] = None
    resolution: str = ""


class ReportListResponse(BaseModel):
    reports: list[ReportEntry]


class AdminReportEntry(ReportEntry):
    reporter_username: str = ""
    reported_username: str = ""
    game_status: str = ""
    game_rated: bool = False
    audit_findings: int = 0


class AdminReportListResponse(BaseModel):
    reports: list[AdminReportEntry]
    total: int


class ReportReviewRequest(BaseModel):
    action: Annotated[str, Field(min_length=1, max_length=16)]
    note: Annotated[str, Field(default="", max_length=500)] = ""

    @field_validator("action")
    @classmethod
    def _action_valid(cls, value: str) -> str:
        allowed = {"dismiss", "warn", "uphold"}
        if value not in allowed:
            raise ValueError("处理动作必须是 dismiss/warn/uphold")
        return value


class AuditFindingEntry(BaseModel):
    code: str
    severity: str
    message: str


class GameAuditResponse(BaseModel):
    game_id: str
    ok: bool
    move_count: int
    findings: list[AuditFindingEntry]
    summary: dict[str, int] = Field(default_factory=dict)
    generated_at: datetime


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
    "SeasonInfoResponse",
    "SeasonCurrentResponse",
    "SeasonArchiveEntry",
    "SeasonArchiveResponse",
    "ReportCreateRequest",
    "ReportEntry",
    "ReportListResponse",
    "AdminReportEntry",
    "AdminReportListResponse",
    "ReportReviewRequest",
    "AuditFindingEntry",
    "GameAuditResponse",
    "GameMove",
    "RecentGameEntry",
    "RecentGamesResponse",
    "GameDetailResponse",
    "AvatarUpdateRequest",
    "AvatarImageRequest",
    "PublicProfileResponse",
    "UserSearchEntry",
    "UserSearchResponse",
    "FriendEntry",
    "FriendRequestEntry",
    "FriendsResponse",
    "FriendRequestCreate",
    "DirectMessageEntry",
    "DirectMessagesResponse",
    "SendDirectMessageRequest",
]
