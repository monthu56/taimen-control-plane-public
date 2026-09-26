"""verification stage: attempts of a task's acceptance checks (CP-ADR-0067)

Revision ID: b8d3f1a6c2e9
Revises: a4c7e2f9b1d3
Create Date: 2026-09-25

Schema:

* ``task_verifications`` — one row per attempt of the verification stage: a
  task with acceptance checks that is completed gets an open attempt instead
  of its completion status; the worker executes the checks in declared order
  (``cursor``) with the completer's authority (``authority``) and closes the
  attempt ``passed``, ``failed`` or ``cancelled``. ``checks`` is the
  acceptance as it was when the attempt opened, ``results`` what each check
  did. The partial unique index keeps one open attempt per task, so a repeated
  completion does not open a second one.

Downgrade is lossy: the history of attempts is dropped (tasks keep their
status; an attempt still open leaves its task where the completion left it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8d3f1a6c2e9"
down_revision: str | None = "a4c7e2f9b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN = "status IN ('running', 'waiting_human', 'waiting_external')"


def upgrade() -> None:
    op.create_table(
        "task_verifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("trigger_ref", sa.Text(), nullable=True),
        sa.Column("authority_principal_id", sa.UUID(), nullable=False),
        sa.Column("authority", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("skill_invocation_id", sa.UUID(), nullable=True),
        sa.Column("approval_id", sa.UUID(), nullable=True),
        sa.Column("next_check_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_human', 'waiting_external', 'passed', 'failed', "
            "'cancelled')",
            name=op.f("ck_task_verifications_status"),
        ),
        sa.CheckConstraint(
            "trigger IN ('run', 'complete', 'approval', 'rule')",
            name=op.f("ck_task_verifications_trigger"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_task_verifications_attempt_positive")),
        sa.CheckConstraint("cursor >= 0", name=op.f("ck_task_verifications_cursor_nonnegative")),
        sa.CheckConstraint(
            "jsonb_typeof(checks) = 'array'", name=op.f("ck_task_verifications_checks_is_array")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(results) = 'array'", name=op.f("ck_task_verifications_results_is_array")
        ),
        sa.CheckConstraint(
            f"({_OPEN}) = (finished_at IS NULL)",
            name=op.f("ck_task_verifications_finished_when_closed"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_task_verifications_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_verifications_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["authority_principal_id"],
            ["principals.id"],
            name=op.f("fk_task_verifications_authority_principal_id_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["skill_invocation_id"],
            ["skill_invocations.id"],
            name=op.f("fk_task_verifications_skill_invocation_id_skill_invocations"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name=op.f("fk_task_verifications_approval_id_approvals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_verifications")),
        sa.UniqueConstraint("task_id", "attempt", name="uq_task_verifications_task_attempt"),
    )
    op.create_index(
        "uq_task_verifications_open",
        "task_verifications",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN),
    )
    op.create_index(
        "ix_task_verifications_due",
        "task_verifications",
        ["next_check_at"],
        postgresql_where=sa.text("next_check_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_verifications_due", table_name="task_verifications")
    op.drop_index("uq_task_verifications_open", table_name="task_verifications")
    op.drop_table("task_verifications")
