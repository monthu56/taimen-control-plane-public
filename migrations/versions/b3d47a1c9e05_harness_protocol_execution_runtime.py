"""harness protocol and execution runtime

Revision ID: b3d47a1c9e05
Revises: fc01bd83300c
Create Date: 2026-08-11 21:30:00.000000

v0.3: harness registration on sessions, run suspension/cancellation/budget,
approval gates, artifact revision lineage, run checkpoints and the run action
audit trail.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3d47a1c9e05"
down_revision: str | None = "fc01bd83300c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- sessions: harness registration metadata ------------------------------
    op.add_column("sessions", sa.Column("harness_type", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("harness_version", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("protocol_version", sa.Text(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("harness_capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("sessions", sa.Column("hostname", sa.Text(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("environment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_sessions_principal_active",
        "sessions",
        ["principal_id"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- runs: suspension, cooperative cancel, budget -------------------------
    op.drop_constraint(op.f("ck_runs_status"), "runs", type_="check")
    op.create_check_constraint(
        op.f("ck_runs_status"),
        "runs",
        "status IN ('running', 'succeeded', 'failed', 'cancelled', 'suspended')",
    )
    op.add_column(
        "runs",
        sa.Column("cancel_requested_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("runs", sa.Column("cancel_requested_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_cancel_requested_by_principals"),
        "runs",
        "principals",
        ["cancel_requested_by"],
        ["id"],
    )
    op.add_column("runs", sa.Column("max_duration_seconds", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("max_actions", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_runs_max_duration_positive"),
        "runs",
        "max_duration_seconds IS NULL OR max_duration_seconds > 0",
    )
    op.create_check_constraint(
        op.f("ck_runs_max_actions_positive"), "runs", "max_actions IS NULL OR max_actions > 0"
    )

    # --- task_requirements: exact skill version pinning -----------------------
    op.add_column(
        "task_requirements",
        sa.Column("skill_exact", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # --- artifacts: revision lineage ------------------------------------------
    op.add_column("artifacts", sa.Column("supersedes_artifact_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_artifacts_supersedes_artifact_id_artifacts"),
        "artifacts",
        "artifacts",
        ["supersedes_artifact_id"],
        ["id"],
    )

    # --- approvals: first-class gates -----------------------------------------
    op.add_column(
        "approvals",
        sa.Column("gate", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_check_constraint(
        op.f("ck_approvals_gate_requires_task"), "approvals", "NOT gate OR task_id IS NOT NULL"
    )
    op.create_index(
        "ix_approvals_gate_pending",
        "approvals",
        ["task_id"],
        unique=False,
        postgresql_where=sa.text("gate AND status = 'pending'"),
    )

    # --- run_checkpoints ------------------------------------------------------
    op.create_table(
        "run_checkpoints",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("created_by_principal_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_run_checkpoints_seq_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_run_checkpoints_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_checkpoints_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_run_checkpoints_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["principals.id"],
            name=op.f("fk_run_checkpoints_created_by_principal_id_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_checkpoints")),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_checkpoints_run_seq"),
    )
    op.create_index("ix_run_checkpoints_run", "run_checkpoints", ["run_id"], unique=False)
    op.create_index(
        "ix_run_checkpoints_tenant_created",
        "run_checkpoints",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )

    # --- run_actions ----------------------------------------------------------
    op.create_table(
        "run_actions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("principal_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("skill_id", sa.UUID(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("external_reference", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed')", name=op.f("ck_run_actions_status")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_run_actions_seq_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_run_actions_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_actions_run_id_runs")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_run_actions_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_run_actions_principal_id_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name=op.f("fk_run_actions_session_id_sessions")
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name=op.f("fk_run_actions_skill_id_skills")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_actions")),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_actions_run_seq"),
    )
    op.create_index("ix_run_actions_run", "run_actions", ["run_id"], unique=False)
    op.create_index(
        "ix_run_actions_tenant_created",
        "run_actions",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_run_actions_tenant_created", table_name="run_actions")
    op.drop_index("ix_run_actions_run", table_name="run_actions")
    op.drop_table("run_actions")
    op.drop_index("ix_run_checkpoints_tenant_created", table_name="run_checkpoints")
    op.drop_index("ix_run_checkpoints_run", table_name="run_checkpoints")
    op.drop_table("run_checkpoints")

    op.drop_index("ix_approvals_gate_pending", table_name="approvals")
    op.drop_constraint(op.f("ck_approvals_gate_requires_task"), "approvals", type_="check")
    op.drop_column("approvals", "gate")

    op.drop_constraint(
        op.f("fk_artifacts_supersedes_artifact_id_artifacts"), "artifacts", type_="foreignkey"
    )
    op.drop_column("artifacts", "supersedes_artifact_id")

    op.drop_column("task_requirements", "skill_exact")

    op.drop_constraint(op.f("ck_runs_max_actions_positive"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_max_duration_positive"), "runs", type_="check")
    op.drop_column("runs", "max_actions")
    op.drop_column("runs", "max_duration_seconds")
    op.drop_constraint(op.f("fk_runs_cancel_requested_by_principals"), "runs", type_="foreignkey")
    op.drop_column("runs", "cancel_requested_by")
    op.drop_column("runs", "cancel_requested_at")
    # Suspended runs do not exist in the v0.2 state machine: convert them to
    # failed (audit-preserving) before narrowing the CHECK back.
    op.execute(
        "UPDATE runs SET status = 'failed', "
        "failure_reason = COALESCE(failure_reason, 'suspended_downgraded') "
        "WHERE status = 'suspended'"
    )
    op.drop_constraint(op.f("ck_runs_status"), "runs", type_="check")
    op.create_check_constraint(
        "status", "runs", "status IN ('running', 'succeeded', 'failed', 'cancelled')"
    )

    op.drop_index("ix_sessions_principal_active", table_name="sessions")
    op.drop_column("sessions", "environment")
    op.drop_column("sessions", "hostname")
    op.drop_column("sessions", "harness_capabilities")
    op.drop_column("sessions", "protocol_version")
    op.drop_column("sessions", "harness_version")
    op.drop_column("sessions", "harness_type")
