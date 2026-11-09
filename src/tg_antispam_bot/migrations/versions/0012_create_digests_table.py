from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digests",
        sa.Column(
            "chat_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "recipient_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column("deleted", sa.BigInteger(), nullable=False),
        sa.Column("restricted", sa.BigInteger(), nullable=False),
        sa.Column("banned", sa.BigInteger(), nullable=False),
        sa.Column("latest", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "recipient_id"),
    )


def downgrade() -> None:
    op.drop_table("digests")
