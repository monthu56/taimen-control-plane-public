"""who published an agent revision: a package or a manual edit (CP-ADR-0073, TASK-000868)

Revision ID: a4d8c2e6f1b3
Revises: e3b7d1a9c5f2
Create Date: 2026-09-29

* ``agent_revisions.source_kind`` — ``package`` (the installer named the
  package), ``manual`` (a publication without one) or ``unknown``: every row
  written before this step. The column is added with a default, so the
  existing rows get ``unknown`` without an UPDATE — the immutability trigger
  would reject one — and the default is dropped after.
* ``source_package_key``, ``source_package_version`` — the package of a
  ``package`` revision, empty otherwise.

Downgrade drops the columns: the source of every revision is forgotten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d8c2e6f1b3"
down_revision: str | None = "e3b7d1a9c5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_revisions",
        sa.Column("source_kind", sa.Text(), nullable=False, server_default="unknown"),
    )
    op.alter_column("agent_revisions", "source_kind", server_default=None)
    op.add_column("agent_revisions", sa.Column("source_package_key", sa.Text(), nullable=True))
    op.add_column("agent_revisions", sa.Column("source_package_version", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_agent_revisions_source_kind"),
        "agent_revisions",
        "source_kind IN ('package', 'manual', 'unknown')",
    )
    op.create_check_constraint(
        op.f("ck_agent_revisions_source_package"),
        "agent_revisions",
        "(source_kind = 'package') = (source_package_key IS NOT NULL) "
        "AND (source_package_key IS NULL) = (source_package_version IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_agent_revisions_source_package"), "agent_revisions")
    op.drop_constraint(op.f("ck_agent_revisions_source_kind"), "agent_revisions")
    op.drop_column("agent_revisions", "source_package_version")
    op.drop_column("agent_revisions", "source_package_key")
    op.drop_column("agent_revisions", "source_kind")
