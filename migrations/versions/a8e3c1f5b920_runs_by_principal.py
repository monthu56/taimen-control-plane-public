"""runs listed newest first by start, per executor (CP-ADR-0073 amendment of 2026-09-29)

Revision ID: a8e3c1f5b920
Revises: a4d8c2e6f1b3
Create Date: 2026-09-29

``GET /runs`` orders by ``(started_at, id)`` and filters by ``principalId`` /
``agentKey``:

* ``ix_runs_tenant_principal_started`` — the page of one executor's runs, newest
  first, without sorting the tenant's runs;
* ``ix_runs_tenant_started`` replaces ``ix_runs_tenant_created``: the unfiltered
  list keeps an index for its order. A run is created and started at the same
  instant, so the order of existing rows is the same.

Downgrade restores ``ix_runs_tenant_created`` and drops both.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8e3c1f5b920"
down_revision: str | None = "a4d8c2e6f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_runs_tenant_principal_started",
        "runs",
        ["tenant_id", "principal_id", sa.text("started_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_runs_tenant_started",
        "runs",
        ["tenant_id", sa.text("started_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.drop_index("ix_runs_tenant_created", table_name="runs")


def downgrade() -> None:
    op.create_index(
        "ix_runs_tenant_created", "runs", ["tenant_id", "created_at", "id"], unique=False
    )
    op.drop_index("ix_runs_tenant_started", table_name="runs")
    op.drop_index("ix_runs_tenant_principal_started", table_name="runs")
