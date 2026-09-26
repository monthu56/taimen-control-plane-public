"""skill invocation: single-use approval basis and attempt start (CP-ADR-0056)

Revision ID: c7d3a1f9e2b6
Revises: b5e1d9c3a7f2
Create Date: 2026-09-23

Two changes from the M2.1 review:

1. An approval is a single-use basis for an ``external_write`` call of one
   skill version: a partial unique index over
   ``(skill_id, authorization_basis->>'approvalId')`` for rows whose basis is
   an approval. Concurrent calls citing one approval produce one row.

2. ``attempt_started_at`` — when the current attempt was claimed. A heartbeat
   never extends the lease past ``attempt_started_at + timeoutSeconds * 2 +
   margin``; ``started_at`` keeps meaning "first attempt started". Rows
   claimed before this revision are backfilled from ``started_at``: for a
   retried call that is earlier than the real start of the current attempt,
   so its lease can only get shorter, never unbounded.

Apart from that backfill no data is rewritten. The index cannot be built
while two rows of one version cite the same approval; such rows have to be
resolved by hand before upgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d3a1f9e2b6"
down_revision: str | None = "b5e1d9c3a7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skill_invocations",
        sa.Column("attempt_started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE skill_invocations SET attempt_started_at = started_at "
        "WHERE attempt_started_at IS NULL AND started_at IS NOT NULL"
    )
    op.create_index(
        "uq_skill_invocations_skill_approval",
        "skill_invocations",
        ["skill_id", sa.text("(authorization_basis ->> 'approvalId')")],
        unique=True,
        postgresql_where=sa.text("authorization_basis ->> 'kind' = 'approval'"),
    )


def downgrade() -> None:
    op.drop_index("uq_skill_invocations_skill_approval", table_name="skill_invocations")
    op.drop_column("skill_invocations", "attempt_started_at")
