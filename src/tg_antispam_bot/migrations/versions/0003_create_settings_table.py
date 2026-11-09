from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column(
            "chat_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
