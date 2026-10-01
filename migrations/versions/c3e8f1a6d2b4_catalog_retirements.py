"""catalog_retirements: a process or calendar key out of use (CP-ADR-0074, amendment Zh1)

Revision ID: c3e8f1a6d2b4
Revises: 16a16d12fe3f
Create Date: 2026-09-30

* ``catalog_retirements`` — one row per retired ``(tenant, kind, key)`` of the
  kinds ``Process`` and ``Calendar``: when, by whom and why. It is the only
  mark of retirement: ``POST /process-definitions/{key}:retire``, ``POST
  /calendars/{key}:retire`` and a package renaming a key away write it, a new
  version of the key deletes it.
* ``package_objects.retired_at`` moves there (``retired_by`` — who applied the
  link, ``reason`` — ``renamed by package <key>``) and is dropped.

Downgrade brings the column back from the rows a rename wrote and forgets the
rest: keys retired by ``:retire`` are in use again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e8f1a6d2b4"
down_revision: str | None = "16a16d12fe3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMED = "renamed by package "


def upgrade() -> None:
    op.create_table(
        "catalog_retirements",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("retired_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("retired_by", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('Process', 'Calendar')", name=op.f("ck_catalog_retirements_kind_known")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_catalog_retirements_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["retired_by"], ["principals.id"], name="fk_catalog_retirements_retired_by_principals"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "kind", "key", name="pk_catalog_retirements"),
    )
    op.execute(
        f"""
        INSERT INTO catalog_retirements (tenant_id, kind, key, retired_at, retired_by, reason)
        SELECT tenant_id, kind, key, retired_at, applied_by, '{RENAMED}' || package_key
        FROM package_objects
        WHERE retired_at IS NOT NULL AND kind IN ('Process', 'Calendar')
        """
    )
    op.drop_column("package_objects", "retired_at")


def downgrade() -> None:
    op.add_column(
        "package_objects",
        sa.Column("retired_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        f"""
        UPDATE package_objects p SET retired_at = r.retired_at
        FROM catalog_retirements r
        WHERE r.tenant_id = p.tenant_id AND r.kind = p.kind AND r.key = p.key
          AND r.reason LIKE '{RENAMED}%'
        """
    )
    op.drop_table("catalog_retirements")
