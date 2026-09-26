"""work a task type declares for after completion (CP-ADR-0061, amendment 2026-09-25)

Revision ID: a4c7e2f9b1d3
Revises: ffbbd22726d3
Create Date: 2026-09-25

Schema:

* ``task_types.completion_schema`` — what core files once a task of this
  version is completed: ``{"onComplete": {"when": [...], "actions": [...]}}``,
  checked by the application at publication. Part of the immutable version
  like ``approval_schema``: the immutability trigger is re-created to cover
  it. ``{}`` — nothing after completion, the behaviour before this revision.
* ``task_completion_work`` — one row per completed task whose type declared
  work and whose ``when`` held: what each declared action did, ``executed`` or
  ``failed``. The unique task is what makes the work filed once.

Downgrade is lossy: the declarations and the record of what they filed are
dropped (the work items they filed stay).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c7e2f9b1d3"
down_revision: str | None = "ffbbd22726d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The function as ffbbd22726d3 left it (instructions included), plus completion_schema.
_TASK_TYPE_IMMUTABLE_FN = """
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
        OR NEW.execution IS DISTINCT FROM OLD.execution
        OR NEW.context_schema IS DISTINCT FROM OLD.context_schema
        OR NEW.instructions IS DISTINCT FROM OLD.instructions
        {extra}
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
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
        sa.Column(
            "completion_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        _TASK_TYPE_IMMUTABLE_FN.replace(
            "{extra}", "OR NEW.completion_schema IS DISTINCT FROM OLD.completion_schema"
        )
    )
    op.create_table(
        "task_completion_work",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("task_type_id", sa.UUID(), nullable=False),
        sa.Column("completed_by", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("actions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('executed', 'failed')", name=op.f("ck_task_completion_work_status")
        ),
        sa.CheckConstraint("attempts >= 1", name=op.f("ck_task_completion_work_attempts_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_task_completion_work_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_completion_work_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["task_type_id"],
            ["task_types.id"],
            name=op.f("fk_task_completion_work_task_type_id_task_types"),
        ),
        sa.ForeignKeyConstraint(
            ["completed_by"],
            ["principals.id"],
            name=op.f("fk_task_completion_work_completed_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_completion_work")),
        sa.UniqueConstraint("task_id", name="uq_task_completion_work_task"),
    )
    op.create_index(
        "ix_task_completion_work_tenant", "task_completion_work", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_completion_work_tenant", table_name="task_completion_work")
    op.drop_table("task_completion_work")
    op.execute(_TASK_TYPE_IMMUTABLE_FN.replace("{extra}", ""))
    op.drop_column("task_types", "completion_schema")
