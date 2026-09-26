"""task_types.execution and one skill call per execution run (CP-ADR-0056 §3)

Revision ID: e3a9c5d7f1b4
Revises: e3b8c1a6d9f4
Create Date: 2026-09-23

1. ``task_types.execution`` (JSONB, nullable): ``{skill, version, inputs}``
   when a task of this type is executed by a skill invocation (M2.2). The
   column joins the immutable content of a type version: the trigger function
   ``forbid_task_type_mutation`` is replaced by one that also freezes it.
   Existing rows get NULL — ordinary work, as before.

2. A partial unique index over ``skill_invocations.run_id`` for rows whose
   basis is the task type's execution: a run of such a task makes exactly one
   skill call, even when two requests race.

No data is rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3a9c5d7f1b4"
down_revision: str | None = "e3b8c1a6d9f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _immutable_fn(*, with_execution: bool) -> str:
    execution = (
        "        OR NEW.execution IS DISTINCT FROM OLD.execution\n" if with_execution else ""
    )
    return f"""
CREATE OR REPLACE FUNCTION forbid_task_type_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'task_types rows are immutable (DELETE rejected)';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.key IS DISTINCT FROM OLD.key
        OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.display_name IS DISTINCT FROM OLD.display_name
        OR NEW.description IS DISTINCT FROM OLD.description
        OR NEW.field_schema IS DISTINCT FROM OLD.field_schema
        OR NEW.lifecycle_schema IS DISTINCT FROM OLD.lifecycle_schema
        OR NEW.approval_schema IS DISTINCT FROM OLD.approval_schema
{execution}        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'task_types content is immutable; create a new version';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
        AND NOT (OLD.status = 'active' AND NEW.status = 'deprecated')
    THEN
        RAISE EXCEPTION 'task_types status may only move active -> deprecated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column(
        "task_types",
        sa.Column("execution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(_immutable_fn(with_execution=True))
    op.create_index(
        "uq_skill_invocations_run_execution",
        "skill_invocations",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("authorization_basis ->> 'kind' = 'execution'"),
    )


def downgrade() -> None:
    op.drop_index("uq_skill_invocations_run_execution", table_name="skill_invocations")
    op.execute(_immutable_fn(with_execution=False))
    op.drop_column("task_types", "execution")
