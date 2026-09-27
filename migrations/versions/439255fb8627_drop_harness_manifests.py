"""drop the harness manifests (CP-ADR-0043 superseded by CP-ADR-0073)

Revision ID: 439255fb8627
Revises: d7e2a9c4f1b8
Create Date: 2026-09-27

The configuration an executor ran with is now the agent revision the run names
(``runs.agent_revision_id``), so ``run_harness_manifests`` and
``run_manifest_ephemerals`` lose their purpose and go together with their
immutability triggers and ``reject_manifest_mutation()`` (declarative-agents,
D007).

WARNING for operators: ``upgrade()`` DROPs both tables, i.e. it DESTROYS the
manifests recorded so far. Export them first if they are still needed as
evidence. ``downgrade()`` restores the empty tables and their triggers, not the
rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "439255fb8627"
down_revision: str | None = "d7e2a9c4f1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("run_manifest_ephemerals", "run_harness_manifests")

_HASH_PATTERN = "^sha256:[0-9a-f]{64}$"

_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION reject_manifest_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'run manifest rows are immutable; compile a new version';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_manifest_mutation()")
    for table in _TABLES:
        op.drop_table(table)


def downgrade() -> None:
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
    for table in reversed(_TABLES):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_manifest_mutation();
            """
        )
