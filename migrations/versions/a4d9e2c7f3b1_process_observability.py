"""engine revision, step attempts and SLA deadlines of processes (CP-ADR-0074, CP-ADR-0078)

Revision ID: a4d9e2c7f3b1
Revises: e6b3d8f1a2c9
Create Date: 2026-09-29

* ``process_definitions.engine_revision`` — the semantics revision a version
  runs under (CP-ADR-0074 §3, amendment 2026-09-29): ``1`` for every version
  published before this revision, SLA deadlines act only at ``2``.
* ``process_timers.remaining_unit`` — the unit ``remaining_seconds`` of a
  frozen timer is kept in (``wall | working_seconds | workdays``,
  CP-ADR-0078 §4); every existing timer is ``wall``.
* ``process_instances.step_attempts`` — ``{element: n}``, the attempt number
  of each step (CP-ADR-0074 §13); ``{}`` for existing instances.
* ``process_instances.sla_due_at``, ``sla_warn_at`` — the earliest open
  deadline and warning of an instance, denormalized for
  ``GET /process-instances?slaState=`` (CP-ADR-0078 §6), with a partial index
  over the rows that have a running deadline. ``NULL`` — no deadline, as for
  every existing row.

Every column has a default or is nullable: existing rows are not rewritten
(``process_definitions`` rejects UPDATE by trigger, the defaults fill them).
Downgrade drops the columns and loses what the new code wrote into them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4d9e2c7f3b1"
down_revision: str | None = "e6b3d8f1a2c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "process_definitions",
        sa.Column(
            "engine_revision", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
    )
    op.add_column(
        "process_timers",
        sa.Column("remaining_unit", sa.Text(), server_default=sa.text("'wall'"), nullable=False),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "step_attempts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column("sla_due_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "process_instances",
        sa.Column("sla_warn_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_process_instances_sla_due",
        "process_instances",
        ["tenant_id", "sla_due_at"],
        postgresql_where=sa.text("sla_due_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_process_instances_sla_due", table_name="process_instances")
    op.drop_column("process_instances", "sla_warn_at")
    op.drop_column("process_instances", "sla_due_at")
    op.drop_column("process_instances", "step_attempts")
    op.drop_column("process_timers", "remaining_unit")
    op.drop_column("process_definitions", "engine_revision")
