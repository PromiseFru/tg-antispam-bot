from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "actions_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_name", sa.String(), nullable=False),
        sa.Column("score", sa.BigInteger(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("message_text", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("until_ts", sa.BigInteger(), nullable=True),
        sa.Column("deleted_count", sa.BigInteger(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by_name", sa.String(), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("actions_log", schema=None) as batch_op:
        batch_op.create_index(
            "idx_actions_chat_time", ["chat_id", "created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("actions_log", schema=None) as batch_op:
        batch_op.drop_index("idx_actions_chat_time")

    op.drop_table("actions_log")
