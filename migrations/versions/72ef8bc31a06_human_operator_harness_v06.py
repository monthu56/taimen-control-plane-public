"""v0.6: server-derived Harness Session control level.

Revision ID: 72ef8bc31a06
Revises: 1adf50721f1e

Existing sessions are conservatively classified as ``connected``. New rows
are classified by application code from the authenticated Principal kind;
the database default exists for legacy writers and is intentionally neutral.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "72ef8bc31a06"
down_revision: str | None = "1adf50721f1e"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "control_level",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'connected'"),
        ),
    )
    op.create_check_constraint(
        "ck_sessions_control_level",
        "sessions",
        "control_level IN ('managed', 'connected', 'human_operated')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sessions_control_level", "sessions", type_="check")
    op.drop_column("sessions", "control_level")
