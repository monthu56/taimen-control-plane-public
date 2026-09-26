"""M1.3 work derivation rules: work_rules, rule_evaluations, rule_work_items (CP-ADR-0063)

Revision ID: f4c1e8b2a9d7
Revises: d2f7a3c9b1e5
Create Date: 2026-09-24

Schema:

* ``work_rules`` — tenant data: ``trigger`` / ``condition`` /
  ``interpretation`` / ``action`` documents (validated by the application,
  ``domain/work_rules.py``; the database only checks that trigger and action
  are objects with a ``kind``), ``status`` ``enabled|disabled|archived``, a
  ``version`` that grows with every change, and the credential snapshot the
  rule acts with (``authority``). The key is unique per tenant among rules
  that are not archived (partial unique index). ``goal_id`` is a
  tenant-consistent composite FK to ``goals``.
* ``rule_evaluations`` — one row per ``(rule_id, trigger_ref)``: the
  idempotency key of the journal consumer. ``next_check_at`` is set exactly
  while the evaluation waits for a skill call (CHECK), a partial index serves
  the worker's scan of waiting evaluations.
* ``rule_work_items`` — append-only ledger ``(tenant, dedup key) → task`` (and
  the rule that filed it).

Data: none — no rule exists before this revision. The journal consumer's
cursor lives in the existing ``event_consumer_cursors`` table under the name
``work-rules``; its rows are created with the first rule of a tenant.

Downgrade is lossy: rules, their evaluation history and the ledger are
dropped (the work items they created stay, with ``origin.kind = rule``), as
are the consumer's cursor rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4c1e8b2a9d7"
down_revision: str | None = "d2f7a3c9b1e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "work_rules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("goal_id", sa.UUID(), nullable=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("trigger", _JSONB, nullable=False),
        sa.Column("condition", _JSONB, nullable=False),
        sa.Column("interpretation", _JSONB, nullable=True),
        sa.Column("action", _JSONB, nullable=False),
        sa.Column("authority", _JSONB, nullable=True),
        sa.Column("authority_principal_id", sa.UUID(), nullable=True),
        sa.Column("enabled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('enabled', 'disabled', 'archived')", name=op.f("ck_work_rules_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_work_rules_version_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(trigger) = 'object' AND trigger ? 'kind'",
            name=op.f("ck_work_rules_trigger"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(action) = 'object' AND action ? 'kind'",
            name=op.f("ck_work_rules_action"),
        ),
        sa.CheckConstraint(
            "status <> 'enabled' OR (enabled_at IS NOT NULL AND authority_principal_id "
            "IS NOT NULL)",
            name=op.f("ck_work_rules_enabled_has_authority"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_work_rules_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_work_rules_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "goal_id"], ["goals.tenant_id", "goals.id"], name="fk_work_rules_goal"
        ),
        sa.ForeignKeyConstraint(
            ["authority_principal_id"],
            ["principals.id"],
            name=op.f("fk_work_rules_authority_principal_id_principals"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name=op.f("fk_work_rules_created_by_principals")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_rules")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_work_rules_tenant_id_id"),
    )
    op.create_index(
        "uq_work_rules_live_key",
        "work_rules",
        ["tenant_id", "key"],
        unique=True,
        postgresql_where=sa.text("status <> 'archived'"),
    )
    op.create_index("ix_work_rules_tenant_created", "work_rules", ["tenant_id", "created_at", "id"])
    op.create_index(
        "ix_work_rules_schedule_due",
        "work_rules",
        ["next_run_at"],
        postgresql_where=sa.text("status = 'enabled' AND next_run_at IS NOT NULL"),
    )

    op.create_table(
        "rule_evaluations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("trigger_ref", sa.Text(), nullable=False),
        sa.Column("trigger_event_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("skill_invocation_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_task_ids", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("error", _JSONB, nullable=True),
        sa.Column("next_check_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('waiting', 'matched', 'not_matched', 'failed', 'skipped')",
            name=op.f("ck_rule_evaluations_status"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'array'", name=op.f("ck_rule_evaluations_evidence_is_array")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(created_task_ids) = 'array'",
            name=op.f("ck_rule_evaluations_created_task_ids_is_array"),
        ),
        sa.CheckConstraint(
            "(status = 'waiting') = (next_check_at IS NOT NULL)",
            name=op.f("ck_rule_evaluations_waiting_has_next_check"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_rule_evaluations_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["work_rules.id"], name=op.f("fk_rule_evaluations_rule_id_work_rules")
        ),
        sa.ForeignKeyConstraint(
            ["skill_invocation_id"],
            ["skill_invocations.id"],
            name=op.f("fk_rule_evaluations_skill_invocation_id_skill_invocations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_evaluations")),
        sa.UniqueConstraint("rule_id", "trigger_ref", name="uq_rule_evaluations_trigger"),
    )
    op.create_index(
        "ix_rule_evaluations_rule_created", "rule_evaluations", ["rule_id", "created_at", "id"]
    )
    op.create_index(
        "ix_rule_evaluations_waiting",
        "rule_evaluations",
        ["next_check_at"],
        postgresql_where=sa.text("status = 'waiting'"),
    )

    op.create_table(
        "rule_work_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("rule_id", sa.UUID(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_rule_work_items_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["work_rules.id"], name=op.f("fk_rule_work_items_rule_id_work_rules")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_rule_work_items_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["rule_evaluations.id"],
            name=op.f("fk_rule_work_items_evaluation_id_rule_evaluations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_work_items")),
        sa.UniqueConstraint("task_id", name="uq_rule_work_items_task"),
    )
    op.create_index(
        "ix_rule_work_items_key", "rule_work_items", ["tenant_id", "dedup_key", "created_at"]
    )


def downgrade() -> None:
    op.execute("DELETE FROM event_consumer_cursors WHERE name = 'work-rules'")
    op.drop_index("ix_rule_work_items_key", table_name="rule_work_items")
    op.drop_table("rule_work_items")
    op.drop_index("ix_rule_evaluations_waiting", table_name="rule_evaluations")
    op.drop_index("ix_rule_evaluations_rule_created", table_name="rule_evaluations")
    op.drop_table("rule_evaluations")
    op.drop_index("ix_work_rules_schedule_due", table_name="work_rules")
    op.drop_index("ix_work_rules_tenant_created", table_name="work_rules")
    op.drop_index("uq_work_rules_live_key", table_name="work_rules")
    op.drop_table("work_rules")
