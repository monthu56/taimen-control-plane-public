"""task context profile and task context packs (CP-ADR-0064, TAI-ADR-0042 P2)

Revision ID: b7e4d2a9c6f1
Revises: f4c1e8b2a9d7
Create Date: 2026-09-25

Schema:

* ``task_types.context_schema`` — the context profile of a type version
  (anchors, traverse, asOf, budgetTokens). Part of the immutable version like
  ``approval_schema``: the immutability trigger is re-created to cover it.
  ``{}`` — no profile, the behaviour before this revision.
* ``task_context_packs`` — what the Context Compiler was asked and what it
  used, per claim: the typed request as sent (anchors, traverse, the pinned
  moment), the namespaces, and the entities, facts and snapshot ids of the
  answer. The row is the pack's link to its task and claim (the task document
  is not written; evidence may cite a row explicitly, ``{"kind":
  "context_pack"}``), so the pack is reproducible: the same request against
  the same moment. One row per claim (unique), append-only like the comment
  history.

Only this revision's own tables and columns are touched; nothing here depends
on tables of parallel branches.

Downgrade is lossy: the profiles and the pack records are dropped. Evidence
items of kind ``context_pack`` cited explicitly stay in ``tasks.evidence`` as
dangling pointers, since rewriting task documents in a downgrade would be worse.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e4d2a9c6f1"
down_revision: str | None = "f4c1e8b2a9d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The function as e3a9c5d7f1b4 left it (execution included), plus context_schema.
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

_PACK_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION forbid_task_context_pack_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'task_context_packs are append-only (% rejected)', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;
"""


def upgrade() -> None:
    op.add_column(
        "task_types",
        sa.Column(
            "context_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        _TASK_TYPE_IMMUTABLE_FN.replace(
            "{extra}", "OR NEW.context_schema IS DISTINCT FROM OLD.context_schema"
        )
    )

    op.create_table(
        "task_context_packs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("task_type_id", sa.UUID(), nullable=False),
        sa.Column("compiled_by", sa.UUID(), nullable=False),
        sa.Column("as_of", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("as_of_mode", sa.Text(), nullable=False),
        sa.Column("namespaces", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("used", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unresolved", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("budget_tokens", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "as_of_mode IN ('taskCreated', 'now', 'origin')",
            name=op.f("ck_task_context_packs_as_of_mode"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_task_context_packs_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_context_packs_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["task_claims.id"],
            name=op.f("fk_task_context_packs_claim_id_task_claims"),
        ),
        sa.ForeignKeyConstraint(
            ["task_type_id"],
            ["task_types.id"],
            name=op.f("fk_task_context_packs_task_type_id_task_types"),
        ),
        sa.ForeignKeyConstraint(
            ["compiled_by"],
            ["principals.id"],
            name=op.f("fk_task_context_packs_compiled_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_context_packs")),
        sa.UniqueConstraint("claim_id", name="uq_task_context_packs_claim"),
    )
    op.create_index(
        "ix_task_context_packs_task", "task_context_packs", ["tenant_id", "task_id", "created_at"]
    )
    # A record of what an executor was shown is not something its subject may
    # rewrite afterwards.
    op.execute(_PACK_APPEND_ONLY_FN)
    op.execute(
        "CREATE TRIGGER task_context_packs_append_only "
        "BEFORE UPDATE OR DELETE ON task_context_packs "
        "FOR EACH ROW EXECUTE FUNCTION forbid_task_context_pack_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER task_context_packs_append_only ON task_context_packs")
    op.drop_index("ix_task_context_packs_task", table_name="task_context_packs")
    op.drop_table("task_context_packs")
    op.execute("DROP FUNCTION forbid_task_context_pack_mutation()")
    op.execute(_TASK_TYPE_IMMUTABLE_FN.replace("{extra}", ""))
    op.drop_column("task_types", "context_schema")
