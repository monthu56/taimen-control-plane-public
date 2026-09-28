"""process recall queue and the context profile of a step's task (CP-ADR-0076, P010)

Revision ID: c5a2e8d4f7b3
Revises: e3b7c1d9a4f2
Create Date: 2026-09-28

* ``process_recalls`` — the ``recall`` intents of instances, executed by the
  worker after the step's transaction (an outbox): ``id`` is the engine's
  ``recallId``, ``request`` the intent as the engine made it. ``pending``
  rows are picked up ``FOR UPDATE SKIP LOCKED`` once ``next_attempt_at`` has
  come; ``answered`` — the answer (or the refusal) went into the instance
  journal; ``closed`` — the step stopped waiting before memory answered.
* ``tasks.context_profile`` — the ``context`` of a process step whose task
  this is, with its anchors computed; it replaces the profile of the task's
  type. ``NULL`` — the profile of the type, exactly as before this revision.

Downgrade is lossy: pending recalls are forgotten (their steps time out),
tasks of steps fall back to the profile of their type.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5a2e8d4f7b3"
down_revision: str | None = "e3b7c1d9a4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "process_recalls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("instance_id", sa.UUID(), nullable=False),
        sa.Column("element", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("answered_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'answered', 'closed')",
            name=op.f("ck_process_recalls_state_known"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_process_recalls_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["process_instances.id"],
            name="fk_process_recalls_instance_id_process_instances",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_recalls"),
    )
    op.create_index(
        "ix_process_recalls_due",
        "process_recalls",
        ["next_attempt_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index("ix_process_recalls_instance", "process_recalls", ["instance_id"])
    op.add_column(
        "tasks",
        sa.Column("context_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "context_profile")
    op.drop_index("ix_process_recalls_instance", table_name="process_recalls")
    op.drop_index("ix_process_recalls_due", table_name="process_recalls")
    op.drop_table("process_recalls")
