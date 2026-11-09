from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_prefs",
        sa.Column(
            "user_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("admin_prefs")
