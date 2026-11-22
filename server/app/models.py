"""数据库模型：账号、刷新会话、对局与落子。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from gomoku.shared.avatar import DEFAULT_AVATAR
from server.app.db import Base, utcnow_naive
from server.app.ratings import START_RATING


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_key: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar: Mapped[str] = mapped_column(
        String(16),
        default=DEFAULT_AVATAR,
        server_default=text(f"'{DEFAULT_AVATAR}'"),
        nullable=False,
    )
    # 自定义头像：JPEG data URL（含 data:image/jpeg;base64, 前缀）。
    # 体积上限由 shared.avatar 与接口层共同约束；为空时使用预置印章。
    # 延迟加载：排行榜/好友列表每次会读取大量 User 行，头像图片不应该
    # 跟着每一个账号查询被搬进内存；只有头像读取接口显式查询这一列。
    avatar_image: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, default=None, deferred=True
    )
    # 是否存在自定义图片。``avatar_image`` 被延迟加载，排行榜/好友列表
    # 只读这个布尔列即可判断是否需要向客户端下发头像版本。
    has_avatar_image: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    # 内部修订号：每次上传/移除自定义头像都会递增，保证图片 URL 的
    # 版本号永不复用（避免清空后再上传时客户端命中旧缓存）。
    # 对外只暴露 public_avatar_version：没有图片时恒为 0。
    avatar_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    @property
    def public_avatar_version(self) -> int:
        """对外可见的头像版本号；没有自定义图片时为 0。"""
        if not self.has_avatar_image:
            return 0
        return max(0, self.avatar_version)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    # 排位积分与战绩（仅排位匹配对局计入，好友房不计分）。
    rating: Mapped[int] = mapped_column(
        Integer,
        default=START_RATING,
        server_default=text(str(START_RATING)),
        index=True,
        nullable=False,
    )
    peak_rating: Mapped[int] = mapped_column(
        Integer,
        default=START_RATING,
        server_default=text(str(START_RATING)),
        nullable=False,
    )
    rated_games: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    wins: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    losses: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    draws: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # 连胜统计用于成就推导；只有排位对局会更新。
    current_win_streak: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    best_win_streak: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    replaced_by_session_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    room_code: Mapped[str] = mapped_column(String(12), index=True, nullable=False)
    black_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    white_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(24), default="waiting", index=True, nullable=False
    )
    winner_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    result_reason: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    move_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    turn_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    rated: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    rating_applied: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )


class Move(Base):
    __tablename__ = "moves"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id"), index=True, nullable=False
    )
    move_no: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    color: Mapped[str] = mapped_column(String(8), nullable=False)
    row: Mapped[int] = mapped_column(Integer, nullable=False)
    col: Mapped[int] = mapped_column(Integer, nullable=False)
    client_move_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("game_id", "move_no", name="uq_moves_game_move_no"),
        UniqueConstraint(
            "game_id", "client_move_id", name="uq_moves_game_client_id"
        ),
    )


class RatingEvent(Base):
    """一局排位赛对某位玩家的积分结算记录（审计/最近战绩）。"""

    __tablename__ = "rating_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    opponent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    score: Mapped[str] = mapped_column(String(8), nullable=False)  # win/loss/draw
    rating_before: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_after: Mapped[int] = mapped_column(Integer, nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("game_id", "user_id", name="uq_rating_events_game_user"),
    )


class Friendship(Base):
    """好友关系：user_a/user_b 为排序后的用户对，requested_by 记录申请人。"""

    __tablename__ = "friendships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_a: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    user_b: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    requested_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_a", "user_b", name="uq_friendships_pair"),
    )


class DirectMessage(Base):
    """好友私聊消息，用于历史记录与离线补收。"""

    __tablename__ = "direct_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sender_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    recipient_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), index=True, nullable=False
    )
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    __table_args__ = (
        Index(
            "ix_direct_messages_pair",
            "sender_id",
            "recipient_id",
            "created_at",
        ),
        # 未读计数按 (recipient_id, read_at) 过滤并按 sender_id 分组；
        # 没有这条索引时，每次刷新好友列表都要扫全表。
        Index(
            "ix_direct_messages_unread",
            "recipient_id",
            "read_at",
            "sender_id",
        ),
    )


__all__ = [
    "User",
    "RefreshSession",
    "Game",
    "Move",
    "RatingEvent",
    "Friendship",
    "DirectMessage",
]
