"""what a package apply last wrote of a process or calendar (CP-ADR-0074 §11, P015)

Revision ID: d7f2a9c4e1b8
Revises: c5a2e8d4f7b3
Create Date: 2026-09-28

* ``package_objects`` — one row per ``(tenant, kind, key)`` of the kinds
  ``POST /packages:apply`` writes (``Process``, ``Calendar``): the package, the
  version and the spec the apply published, the plan it applied. The plan
  compares the latest version with this spec to name the owner of each field
  (``package`` or ``console``). ``retired_at`` — the key was renamed away:
  a retired process starts no new instances.

Downgrade forgets the records: every field is the package's again, retired
processes start instances again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7f2a9c4e1b8"
down_revision: str | None = "c5a2e8d4f7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "package_objects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("package_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("applied_by", sa.UUID(), nullable=False),
        sa.Column("applied_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("retired_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('Process', 'Calendar')", name=op.f("ck_package_objects_kind_known")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_package_objects_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["applied_by"], ["principals.id"], name="fk_package_objects_applied_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_package_objects"),
        sa.UniqueConstraint("tenant_id", "kind", "key", name="uq_package_objects_tenant_kind_key"),
    )


def downgrade() -> None:
    op.drop_table("package_objects")
