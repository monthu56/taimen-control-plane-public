"""observation_dedup_keys: idempotent external observations (CP-ADR-0057)

Revision ID: b3e7d1f9c2a4
Revises: a9c4e2d7f1b3
Create Date: 2026-09-23

A repeated external observation with the same (source, dedupKey) inside a
tenant must resolve to the observation it first produced. The journal stays
the authoritative record; this table only maps the key to that observation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7d1f9c2a4"
down_revision: str | None = "a9c4e2d7f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observation_dedup_keys",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_observation_dedup_keys_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "source", "dedup_key", name="pk_observation_dedup_keys"
        ),
    )


def downgrade() -> None:
    op.drop_table("observation_dedup_keys")
