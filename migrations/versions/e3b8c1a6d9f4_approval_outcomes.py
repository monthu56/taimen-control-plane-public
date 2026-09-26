"""approval outcomes declared by the task type (CP-ADR-0061, TAI-ADR-0041)

Revision ID: e3b8c1a6d9f4
Revises: c7d3a1f9e2b6
Create Date: 2026-09-23

Schema:

* ``task_types.approval_schema`` — part of the immutable version: the
  immutability trigger is re-created to cover it.
* ``approvals.outcome_status`` / ``approvals.decision_authority`` — the state
  of the declared outcome and the deciding credential it runs with;
  ``outcome_attempts`` / ``outcome_next_attempt_at`` / ``outcome_last_error`` —
  the executor's own retry state, independent of outbox delivery.
* ``approval_outcome_actions`` — one row per executed/failed action, keyed by
  ``(approval_id, action_index)``: the executor's idempotency key.

Data: every tenant that already has a ``code-review`` type gets the next
version of it — same fields and lifecycle, plus ``CODE_REVIEW_APPROVAL_SCHEMA``
(reject -> a ``coding-task`` with the fixes on the reviewer's comment, then the
review closes; approve -> the review closes). Tasks resolve a type by key to
its newest active version, so reviews created after the upgrade pick it up;
reviews already open keep the version they were created with. A tenant
without ``code-review`` gets nothing: the type is tenant data, not core's.

Downgrade is lossy: the column, the outcome state and the action log are
dropped; the ``code-review`` versions added here stay (the trigger forbids
deleting a type version, and tasks may already point at it) as plain copies
of the version before them.
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3b8c1a6d9f4"
down_revision: str | None = "c7d3a1f9e2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen copy (a migration never imports application code). The review task's
# own approval decides; `$.spawnedBy` is the coding task the review was spawned
# by, `$.task` is the review task itself. A trailing `!` makes an expression
# required: a review without a source task, an assignee or a published branch
# fails the outcome (`unresolved_expression`) instead of filing an unowned
# "Правки по ревью : " task nobody would pick up.
CODE_REVIEW_APPROVAL_SCHEMA: dict[str, Any] = {
    "gates": {
        "default": {
            "outcomes": {
                "approved": [{"completeTask": {}}],
                "rejected": [
                    {
                        "ensureWork": {
                            "type": "coding-task",
                            "key": "review-fix:$.approval.id",
                            "title": "Правки по ревью $.spawnedBy.publicId!: $.spawnedBy.title",
                            "description": (
                                "Ревью $.task.publicId отклонено. Комментарий ревьюера:\n"
                                "$.approval.comment\n\n"
                                "Продолжи ветку $.spawnedBy.artifact[commit].metadata.branch! "
                                "(не начинай новую от main): исправь замечания ревьюера, "
                                "прогони тесты и опубликуй ветку заново.\n\n"
                                "Исходная задача $.spawnedBy.publicId:\n"
                                "$.spawnedBy.description"
                            ),
                            "assignee": "$.spawnedBy.assigneeId!",
                            "priority": "$.spawnedBy.priority",
                            "workspace": "$.spawnedBy.workspaceId",
                            "relation": {"spawned_by": "$.task.id!"},
                        }
                    },
                    {"completeTask": {}},
                ],
            }
        }
    }
}

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

# The newest version of `code-review` per tenant, unless it already declares
# outcomes (re-running the data step must not stack versions).
_ADD_CODE_REVIEW_VERSION = """
INSERT INTO task_types (
    id, tenant_id, key, version, display_name, description,
    field_schema, lifecycle_schema, approval_schema, status,
    created_by, created_at, updated_at
)
SELECT gen_random_uuid(), latest.tenant_id, latest.key, latest.version + 1,
       latest.display_name, latest.description,
       latest.field_schema, latest.lifecycle_schema, CAST(:schema AS jsonb), 'active',
       latest.created_by, now(), now()
FROM (
    SELECT DISTINCT ON (tenant_id) *
    FROM task_types
    WHERE key = 'code-review'
    ORDER BY tenant_id, version DESC
) AS latest
WHERE latest.approval_schema = '{}'::jsonb
"""


def upgrade() -> None:
    op.add_column(
        "task_types",
        sa.Column(
            "approval_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        _TASK_TYPE_IMMUTABLE_FN.replace(
            "{extra}", "OR NEW.approval_schema IS DISTINCT FROM OLD.approval_schema"
        )
    )

    op.add_column("approvals", sa.Column("outcome_status", sa.Text(), nullable=True))
    op.add_column(
        "approvals",
        sa.Column("decision_authority", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "approvals",
        sa.Column("outcome_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "approvals",
        sa.Column("outcome_next_attempt_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("approvals", sa.Column("outcome_last_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_approvals_outcome_status"),
        "approvals",
        "outcome_status IS NULL OR outcome_status IN ('pending', 'deferred', 'executed', 'failed')",
    )
    # The worker's scan for outcomes that are due: only live ones are indexed.
    op.create_index(
        "ix_approvals_outcome_due",
        "approvals",
        ["outcome_next_attempt_at"],
        postgresql_where=sa.text("outcome_status IN ('pending', 'deferred')"),
    )

    op.create_table(
        "approval_outcome_actions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("approval_id", sa.UUID(), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('executed', 'failed')", name=op.f("ck_approval_outcome_actions_status")
        ),
        sa.CheckConstraint(
            "action_index >= 0",
            name=op.f("ck_approval_outcome_actions_action_index_nonnegative"),
        ),
        sa.CheckConstraint(
            "attempts >= 1", name=op.f("ck_approval_outcome_actions_attempts_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_approval_outcome_actions_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name=op.f("fk_approval_outcome_actions_approval_id_approvals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_outcome_actions")),
        sa.UniqueConstraint(
            "approval_id", "action_index", name="uq_approval_outcome_actions_index"
        ),
    )
    op.create_index(
        "ix_approval_outcome_actions_tenant",
        "approval_outcome_actions",
        ["tenant_id", "approval_id"],
    )

    op.get_bind().execute(
        sa.text(_ADD_CODE_REVIEW_VERSION), {"schema": json.dumps(CODE_REVIEW_APPROVAL_SCHEMA)}
    )


def downgrade() -> None:
    op.drop_index("ix_approval_outcome_actions_tenant", table_name="approval_outcome_actions")
    op.drop_table("approval_outcome_actions")
    op.drop_index("ix_approvals_outcome_due", table_name="approvals")
    op.drop_constraint(op.f("ck_approvals_outcome_status"), "approvals", type_="check")
    op.drop_column("approvals", "outcome_last_error")
    op.drop_column("approvals", "outcome_next_attempt_at")
    op.drop_column("approvals", "outcome_attempts")
    op.drop_column("approvals", "decision_authority")
    op.drop_column("approvals", "outcome_status")
    op.execute(_TASK_TYPE_IMMUTABLE_FN.replace("{extra}", ""))
    op.drop_column("task_types", "approval_schema")
