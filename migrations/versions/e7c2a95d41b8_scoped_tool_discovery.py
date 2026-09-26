"""Scoped tool discovery: index the catalog for bounded search (HRS-3).

The discovery view has no state of its own — the effective policy is recomputed
on every read on purpose. What it needs from the schema is one access path:
every query is scoped to a tenant, skips disabled versions and orders by name,
which is also the pagination key.

Revision ID: e7c2a95d41b8
Revises: d4e6f8a1b2c3
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7c2a95d41b8"
down_revision: str | None = "d4e6f8a1b2c3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_skills_tenant_status_name",
        "skills",
        ["tenant_id", "status", "name", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_skills_tenant_status_name", table_name="skills")
