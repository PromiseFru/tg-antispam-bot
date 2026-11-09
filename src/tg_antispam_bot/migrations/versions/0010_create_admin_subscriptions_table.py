from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_subscriptions",
        sa.Column(
            "chat_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "user_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("inactive_reason", sa.String(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )
    with op.batch_alter_table("admin_subscriptions", schema=None) as batch_op:
        batch_op.create_index(
            "idx_subscriptions_user", ["user_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("admin_subscriptions", schema=None) as batch_op:
        batch_op.drop_index("idx_subscriptions_user")

    op.drop_table("admin_subscriptions")
