"""add games.rule_mode

Revision ID: 0003_game_rule_mode
Revises: 0002_seasons_glicko_reports
Create Date: 2026-11-25 10:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_game_rule_mode"
down_revision: Union[str, Sequence[str], None] = "0002_seasons_glicko_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 对局规则模式：freestyle（自由规则，默认）或 renju（连珠规则）。
    # 已有对局全部按自由规则处理，server_default 会在加列时回填旧行。
    op.add_column(
        "games",
        sa.Column(
            "rule_mode",
            sa.String(length=16),
            server_default=sa.text("'freestyle'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # SQLite 不支持直接 DROP COLUMN，batch 模式会重建表并保留其余列。
    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_column("rule_mode")
