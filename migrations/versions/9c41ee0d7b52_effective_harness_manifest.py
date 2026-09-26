"""v0.7: Effective Harness Manifest for Run (HRS-2).

Revision ID: 9c41ee0d7b52
Revises: 72ef8bc31a06

Two append-only tables. Both carry an immutability trigger for the same reason
``project_config_revisions`` does: a manifest is evidence, and evidence that
can be edited in place proves nothing.

Purely additive — no existing table is touched, so a rolling deploy is safe in
both directions at the code level.

WARNING for operators: ``downgrade()`` DROPs both tables, i.e. it DESTROYS
execution evidence. In production, export ``run_harness_manifests`` and
``run_manifest_ephemerals`` before downgrading (see
``docs/effective-harness-manifest-plan.md``, rollback).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c41ee0d7b52"
down_revision: str | None = "72ef8bc31a06"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_HASH_PATTERN = "^sha256:[0-9a-f]{64}$"

_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION reject_manifest_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'run manifest rows are immutable; compile a new version';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "run_harness_manifests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_hash", sa.Text(), nullable=False),
        sa.Column("snapshot_hash", sa.Text(), nullable=False),
        sa.Column("base", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("captured", postgresql.JSONB(), nullable=False),
        sa.Column("compile_reason", sa.Text(), nullable=False),
        sa.Column("model_attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("supersedes_version", sa.Integer(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_harness_manifests")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_run_harness_manifests_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_harness_manifests_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_run_harness_manifests_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_run_harness_manifests_created_by_principals"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_run_harness_manifests_version_positive")),
        sa.CheckConstraint(
            "model_attempt >= 1", name=op.f("ck_run_harness_manifests_model_attempt_positive")
        ),
        sa.CheckConstraint(
            f"base_hash ~ '{_HASH_PATTERN}'", name=op.f("ck_run_harness_manifests_base_hash")
        ),
        sa.CheckConstraint(
            f"snapshot_hash ~ '{_HASH_PATTERN}'",
            name=op.f("ck_run_harness_manifests_snapshot_hash"),
        ),
        sa.CheckConstraint(
            "compile_reason IN ('run_started', 'recompile', 'provider_fallback')",
            name=op.f("ck_run_harness_manifests_compile_reason"),
        ),
        sa.UniqueConstraint("run_id", "version", name="uq_run_harness_manifests_run_version"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_run_harness_manifests_tenant_id_id"),
    )
    op.create_index(
        "ix_run_harness_manifests_run_version",
        "run_harness_manifests",
        ["run_id", sa.text("version DESC")],
    )
    op.create_index(
        "ix_run_harness_manifests_tenant_created",
        "run_harness_manifests",
        ["tenant_id", "created_at", "id"],
    )
    op.create_index("ix_run_harness_manifests_task", "run_harness_manifests", ["task_id"])

    op.create_table(
        "run_manifest_ephemerals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_manifest_ephemerals")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_run_manifest_ephemerals_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_manifest_ephemerals_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["run_harness_manifests.id"],
            name=op.f("fk_run_manifest_ephemerals_manifest_id_run_harness_manifests"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name=op.f("fk_run_manifest_ephemerals_created_by_principals"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_run_manifest_ephemerals_seq_positive")),
        sa.CheckConstraint(
            "kind IN ('steering', 'warning', 'budget_warning', 'note')",
            name=op.f("ck_run_manifest_ephemerals_kind"),
        ),
        sa.CheckConstraint(
            "char_length(summary) BETWEEN 1 AND 500",
            name=op.f("ck_run_manifest_ephemerals_summary_length"),
        ),
        sa.UniqueConstraint("manifest_id", "seq", name="uq_run_manifest_ephemerals_manifest_seq"),
    )
    op.create_index(
        "ix_run_manifest_ephemerals_manifest", "run_manifest_ephemerals", ["manifest_id", "seq"]
    )
    op.create_index("ix_run_manifest_ephemerals_run", "run_manifest_ephemerals", ["run_id"])

    op.execute(_IMMUTABLE_FN)
    for table in ("run_harness_manifests", "run_manifest_ephemerals"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_manifest_mutation();
            """
        )


def downgrade() -> None:
    for table in ("run_manifest_ephemerals", "run_harness_manifests"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_manifest_mutation()")
    op.drop_table("run_manifest_ephemerals")
    op.drop_table("run_harness_manifests")
