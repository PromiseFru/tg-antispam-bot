<%!
def clean(code):
    lines = [line for line in code.splitlines() if not line.strip().startswith("#")]
    return "\n".join(lines).strip() or "pass"
%>from __future__ import annotations

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${clean(upgrades) if upgrades else "pass"}


def downgrade() -> None:
    ${clean(downgrades) if downgrades else "pass"}
