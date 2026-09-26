"""v0.7: Durable Child Run Handle (HRS-7).

Revision ID: a7f2c4d19b60
Revises: e7c2a95d41b8

Two tables. ``run_child_handles`` is the durable locator of a child execution:
it holds only what has no other home — the launch idempotency key, the
permission ceiling, the cancellation policy, expiry and revocation. Execution
status is deliberately absent: the child Task/Run stay authoritative for that.

``run_child_results`` is append-only evidence and carries the same immutability
trigger as the manifest tables: a result that can be edited in place proves
nothing to the parent that read it.

Purely additive — no existing table is touched, so a rolling deploy is safe in
both directions at the code level.

WARNING for operators: ``downgrade()`` DROPs both tables. Child Tasks, Runs and
``spawned_by`` relations survive, but their permission ceiling and bounded
results are destroyed. Terminate or revoke live handles before downgrading (see
``docs/plans/TASK-000007-child-run-handle.md``, rollback).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7f2c4d19b60"
down_revision: str | None = "e7c2a95d41b8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_HASH_PATTERN = "^sha256:[0-9a-f]{64}$"

_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION reject_child_result_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'child run results are immutable; a terminal result is written once';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "run_child_handles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("relation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("handle_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("granted", postgresql.JSONB(), nullable=False),
        sa.Column("cancellation_policy", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by_principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_by_principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_child_handles")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_run_child_handles_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["runs.id"], name=op.f("fk_run_child_handles_parent_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["tasks.id"], name=op.f("fk_run_child_handles_parent_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["child_task_id"], ["tasks.id"], name=op.f("fk_run_child_handles_child_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id"], ["runs.id"], name=op.f("fk_run_child_handles_child_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["relation_id"],
            ["task_relations.id"],
            name=op.f("fk_run_child_handles_relation_id_task_relations"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["principals.id"],
            name=op.f("fk_run_child_handles_created_by_principal_id_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_principal_id"],
            ["principals.id"],
            name=op.f("fk_run_child_handles_revoked_by_principal_id_principals"),
        ),
        sa.CheckConstraint(
            "handle_version >= 1", name=op.f("ck_run_child_handles_handle_version_positive")
        ),
        sa.CheckConstraint("depth >= 1", name=op.f("ck_run_child_handles_depth_positive")),
        sa.CheckConstraint(
            "cancellation_policy IN ('cascade_cooperative', 'detach')",
            name=op.f("ck_run_child_handles_cancellation_policy"),
        ),
        sa.CheckConstraint(
            f"secret_hash ~ '{_HASH_PATTERN}'", name=op.f("ck_run_child_handles_secret_hash")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_run_child_handles_expiry_after_creation")
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_principal_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_principal_id IS NOT NULL)",
            name=op.f("ck_run_child_handles_revocation_is_attributed"),
        ),
        sa.UniqueConstraint(
            "parent_run_id", "correlation_id", name="uq_run_child_handles_parent_correlation"
        ),
        sa.UniqueConstraint("child_task_id", name="uq_run_child_handles_child_task"),
    )
    op.create_index(
        "uq_run_child_handles_child_run",
        "run_child_handles",
        ["child_run_id"],
        unique=True,
        postgresql_where=sa.text("child_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_run_child_handles_parent_run",
        "run_child_handles",
        ["parent_run_id", "created_at", "id"],
    )
    op.create_index(
        "ix_run_child_handles_tenant_created",
        "run_child_handles",
        ["tenant_id", "created_at", "id"],
    )
    op.create_index(
        "ix_run_child_handles_live",
        "run_child_handles",
        ["parent_run_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "run_child_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("handle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("artifact_refs", postgresql.JSONB(), nullable=False),
        sa.Column("result_hash", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_child_results")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_run_child_results_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["handle_id"],
            ["run_child_handles.id"],
            name=op.f("fk_run_child_results_handle_id_run_child_handles"),
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id"], ["runs.id"], name=op.f("fk_run_child_results_child_run_id_runs")
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'cancelled')",
            name=op.f("ck_run_child_results_outcome"),
        ),
        sa.CheckConstraint(
            f"result_hash ~ '{_HASH_PATTERN}'", name=op.f("ck_run_child_results_result_hash")
        ),
        sa.CheckConstraint(
            "char_length(summary) BETWEEN 1 AND 2000",
            name=op.f("ck_run_child_results_summary_length"),
        ),
        sa.UniqueConstraint("handle_id", name="uq_run_child_results_handle"),
    )
    op.create_index(
        "ix_run_child_results_tenant_recorded",
        "run_child_results",
        ["tenant_id", "recorded_at", "id"],
    )

    op.execute(_IMMUTABLE_FN)
    op.execute(
        """
        CREATE TRIGGER trg_run_child_results_immutable
        BEFORE UPDATE OR DELETE ON run_child_results
        FOR EACH ROW EXECUTE FUNCTION reject_child_result_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_run_child_results_immutable ON run_child_results")
    op.execute("DROP FUNCTION IF EXISTS reject_child_result_mutation()")
    op.drop_table("run_child_results")
    op.drop_table("run_child_handles")
