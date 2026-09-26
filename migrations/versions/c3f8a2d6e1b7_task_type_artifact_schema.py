"""artifacts a task type takes in and hands on (CP-ADR-0072 §7)

Revision ID: c3f8a2d6e1b7
Revises: b9d4f2a7c1e5
Create Date: 2026-09-26

* ``task_types.artifact_schema`` — ``{"inputs": [...], "outputs": [...]}``,
  checked by the application at publication against the artifact type
  registry. Part of the immutable version like ``completion_schema``: the
  immutability trigger is re-created to cover it. ``{}`` — no inputs and no
  outputs, the behaviour before this revision.

Downgrade is lossy: the declarations are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3f8a2d6e1b7"
down_revision: str | None = "b9d4f2a7c1e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The function as a4c7e2f9b1d3 left it (completion_schema included), plus artifact_schema.
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
        OR NEW.completion_schema IS DISTINCT FROM OLD.completion_schema
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
            "artifact_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        _TASK_TYPE_IMMUTABLE_FN.replace(
            "{extra}", "OR NEW.artifact_schema IS DISTINCT FROM OLD.artifact_schema"
        )
    )


def downgrade() -> None:
    op.execute(_TASK_TYPE_IMMUTABLE_FN.replace("{extra}", ""))
    op.drop_column("task_types", "artifact_schema")
