from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_migrations",
        sa.Column(
            "new_chat_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("old_chat_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("new_chat_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_migrations")
