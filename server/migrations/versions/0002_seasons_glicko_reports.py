"""seasons, glicko-2 fields, time controls and game reports

Revision ID: 0002_seasons_glicko_reports
Revises: 0001_baseline
Create Date: 2026-11-23 13:10:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_seasons_glicko_reports"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column(
            "rating_rd",
            sa.Float(),
            server_default=sa.text("100.0"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "rating_volatility",
            sa.Float(),
            server_default=sa.text("0.06"),
            nullable=False,
        ),
    )

    op.add_column(
        "games",
        sa.Column(
            "time_mode",
            sa.String(length=16),
            server_default=sa.text("'per_move'"),
            nullable=False,
        ),
    )
    op.add_column(
        "games",
        sa.Column(
            "main_seconds",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "games",
        sa.Column(
            "increment_seconds",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.create_table(
        "seasons",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_seasons_number"), "seasons", ["number"], unique=True
    )

    op.create_table(
        "season_ratings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("season_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("peak_rating", sa.Integer(), nullable=False),
        sa.Column("rating_rd", sa.Float(), nullable=False),
        sa.Column("rating_volatility", sa.Float(), nullable=False),
        sa.Column("rated_games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("losses", sa.Integer(), nullable=False),
        sa.Column("draws", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "season_id", "user_id", name="uq_season_ratings_season_user"
        ),
    )
    op.create_index(
        op.f("ix_season_ratings_season_id"),
        "season_ratings",
        ["season_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_season_ratings_user_id"),
        "season_ratings",
        ["user_id"],
        unique=False,
    )

    op.add_column(
        "rating_events",
        sa.Column("rating_rd_before", sa.Float(), nullable=True),
    )
    op.add_column(
        "rating_events",
        sa.Column("rating_rd_after", sa.Float(), nullable=True),
    )
    op.add_column(
        "rating_events",
        sa.Column("rating_volatility_before", sa.Float(), nullable=True),
    )
    op.add_column(
        "rating_events",
        sa.Column("rating_volatility_after", sa.Float(), nullable=True),
    )
    # SQLite 不支持给已有表追加外键约束；season_id 只建索引，
    # 赛季归属由应用层写入并在归档查询中校验。
    op.add_column(
        "rating_events",
        sa.Column("season_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        op.f("ix_rating_events_season_id"),
        "rating_events",
        ["season_id"],
        unique=False,
    )

    op.create_table(
        "game_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("reporter_id", sa.String(length=36), nullable=False),
        sa.Column("reported_user_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column(
            "description",
            sa.String(length=500),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewer_id", sa.String(length=36), nullable=True),
        sa.Column(
            "resolution",
            sa.String(length=500),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["reported_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reporter_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game_id", "reporter_id", name="uq_game_reports_game_reporter"
        ),
    )
    op.create_index(
        op.f("ix_game_reports_game_id"),
        "game_reports",
        ["game_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_game_reports_reported_user_id"),
        "game_reports",
        ["reported_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_game_reports_reporter_id"),
        "game_reports",
        ["reporter_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_game_reports_status"),
        "game_reports",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_game_reports_status_created",
        "game_reports",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_game_reports_status_created", table_name="game_reports"
    )
    op.drop_index(
        op.f("ix_game_reports_status"), table_name="game_reports"
    )
    op.drop_index(
        op.f("ix_game_reports_reporter_id"), table_name="game_reports"
    )
    op.drop_index(
        op.f("ix_game_reports_reported_user_id"), table_name="game_reports"
    )
    op.drop_index(
        op.f("ix_game_reports_game_id"), table_name="game_reports"
    )
    op.drop_table("game_reports")

    op.drop_index(
        op.f("ix_rating_events_season_id"), table_name="rating_events"
    )
    op.drop_column("rating_events", "season_id")
    op.drop_column("rating_events", "rating_volatility_after")
    op.drop_column("rating_events", "rating_volatility_before")
    op.drop_column("rating_events", "rating_rd_after")
    op.drop_column("rating_events", "rating_rd_before")

    op.drop_index(
        op.f("ix_season_ratings_user_id"), table_name="season_ratings"
    )
    op.drop_index(
        op.f("ix_season_ratings_season_id"), table_name="season_ratings"
    )
    op.drop_table("season_ratings")

    op.drop_index(op.f("ix_seasons_number"), table_name="seasons")
    op.drop_table("seasons")

    op.drop_column("games", "increment_seconds")
    op.drop_column("games", "main_seconds")
    op.drop_column("games", "time_mode")

    op.drop_column("users", "rating_volatility")
    op.drop_column("users", "rating_rd")
