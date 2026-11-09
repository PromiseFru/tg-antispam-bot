from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_dms",
        sa.Column(
            "recipient_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "message_id", sa.BigInteger(), autoincrement=False, nullable=False
        ),
        sa.Column("action_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("recipient_id", "message_id"),
    )
    with op.batch_alter_table("action_dms", schema=None) as batch_op:
        batch_op.create_index(
            "idx_action_dms_action", ["action_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("action_dms", schema=None) as batch_op:
        batch_op.drop_index("idx_action_dms_action")

    op.drop_table("action_dms")
