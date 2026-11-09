from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "member_activity",
        sa.Column(
            "chat_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "user_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("message_count", sa.BigInteger(), nullable=False),
        sa.Column("last_seen", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("member_activity")
