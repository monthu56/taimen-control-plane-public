"""separation of duties on an approval (CP-ADR-0074 section 7, process-packages P008)

Revision ID: e3b7c1d9a4f2
Revises: b8e3f1c6d2a9
Create Date: 2026-09-27

* ``approvals.excluded_principals`` — JSON array of principal ids whose
  decision the core refuses (``403 separation_of_duties_violation``) on every
  path: engine, console, channel, MCP. ``[]`` — nobody is excluded, exactly an
  approval as it was before this revision.

Downgrade is lossy: pending approvals forget whom they exclude.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3b7c1d9a4f2"
down_revision: str | None = "b8e3f1c6d2a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column(
            "excluded_principals",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("approvals", "excluded_principals")
