from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "joins",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("joins", schema=None) as batch_op:
        batch_op.create_index(
            "idx_joins_chat_time", ["chat_id", "joined_at"], unique=False
        )
        batch_op.create_index(
            "idx_joins_chat_user",
            ["chat_id", "user_id", "joined_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("joins", schema=None) as batch_op:
        batch_op.drop_index("idx_joins_chat_user")
        batch_op.drop_index("idx_joins_chat_time")

    op.drop_table("joins")
