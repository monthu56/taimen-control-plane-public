"""artifact types: a versioned immutable registry (CP-ADR-0072 §6)

Revision ID: b9d4f2a7c1e5
Revises: e7a3c5f1d9b4
Create Date: 2026-09-26

* ``artifact_types`` — a tenant-scoped registry shaped like ``task_types``
  (ADR-0048), with the same database-enforced immutability: a version never
  changes after INSERT except ``status: active -> deprecated``.
``artifacts.type_version`` — the version of the registered type an artifact
was checked against — comes with the content columns in e7a3c5f1d9b4; this
revision starts filling it.

Downgrade drops the registry; ``artifacts.type_version`` keeps the versions
already recorded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9d4f2a7c1e5"
down_revision: str | None = "e7a3c5f1d9b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ARTIFACT_TYPE_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_artifact_type_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'artifact_types rows are immutable (DELETE rejected)';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.key IS DISTINCT FROM OLD.key
        OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.display_name IS DISTINCT FROM OLD.display_name
        OR NEW.description IS DISTINCT FROM OLD.description
        OR NEW.metadata_schema IS DISTINCT FROM OLD.metadata_schema
        OR NEW.media_types IS DISTINCT FROM OLD.media_types
        OR NEW.max_bytes IS DISTINCT FROM OLD.max_bytes
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'artifact_types content is immutable; create a new version';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
        AND NOT (OLD.status = 'active' AND NEW.status = 'deprecated')
    THEN
        RAISE EXCEPTION 'artifact_types status may only move active -> deprecated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "artifact_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "metadata_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("media_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated')", name=op.f("ck_artifact_types_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_artifact_types_version_positive")),
        sa.CheckConstraint("max_bytes >= 1", name=op.f("ck_artifact_types_max_bytes_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_artifact_types_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_artifact_types_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_types"),
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_artifact_types_key_version"),
    )
    op.create_index(
        "ix_artifact_types_tenant_created", "artifact_types", ["tenant_id", "created_at", "id"]
    )
    op.execute(_ARTIFACT_TYPE_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER artifact_types_immutable BEFORE UPDATE OR DELETE ON artifact_types "
        "FOR EACH ROW EXECUTE FUNCTION forbid_artifact_type_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS artifact_types_immutable ON artifact_types")
    op.execute("DROP FUNCTION IF EXISTS forbid_artifact_type_mutation()")
    op.drop_index("ix_artifact_types_tenant_created", table_name="artifact_types")
    op.drop_table("artifact_types")
