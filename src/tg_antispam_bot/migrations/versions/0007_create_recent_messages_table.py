from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recent_messages",
        sa.Column(
            "chat_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "user_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "message_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "user_id", "message_id"),
    )
    with op.batch_alter_table("recent_messages", schema=None) as batch_op:
        batch_op.create_index(
            "idx_recent_messages_time", ["created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("recent_messages", schema=None) as batch_op:
        batch_op.drop_index("idx_recent_messages_time")

    op.drop_table("recent_messages")
