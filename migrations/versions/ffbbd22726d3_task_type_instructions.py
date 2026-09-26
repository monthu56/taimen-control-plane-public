"""task type instructions and the instructions a run started under (CP-ADR-0066)

Revision ID: ffbbd22726d3
Revises: b7e4d2a9c6f1
Create Date: 2026-09-25

Schema:

* ``task_types.instructions`` — how to execute a task of this version, Markdown
  up to 16 KiB (checked by the application at publication). Part of the
  immutable version like ``approval_schema`` and ``context_schema``: the
  immutability trigger is re-created to cover it. ``''`` — no instructions,
  the behaviour before this revision.
* ``runs.instructions_hash`` / ``runs.instructions_refs`` — the sha256 of the
  executor instructions assembled at run start and the version of each layer
  (platform contract, project config revision, task type version). NULL for
  runs started before this revision.

Downgrade is lossy: the instructions of type versions and the run records of
them are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ffbbd22726d3"
down_revision: str | None = "b7e4d2a9c6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The function as b7e4d2a9c6f1 left it (context_schema included), plus instructions.
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
        sa.Column("instructions", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.execute(
        _TASK_TYPE_IMMUTABLE_FN.replace(
            "{extra}", "OR NEW.instructions IS DISTINCT FROM OLD.instructions"
        )
    )
    op.add_column("runs", sa.Column("instructions_hash", sa.Text(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("instructions_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "instructions_refs")
    op.drop_column("runs", "instructions_hash")
    op.execute(_TASK_TYPE_IMMUTABLE_FN.replace("{extra}", ""))
    op.drop_column("task_types", "instructions")
