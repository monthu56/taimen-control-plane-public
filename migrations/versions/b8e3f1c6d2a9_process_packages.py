"""process-packages: working-day calendars and process definitions (CP-ADR-0074)

Revision ID: b8e3f1c6d2a9
Revises: a7c4e2d9b3f1
Create Date: 2026-09-27

* ``calendars`` — immutable versions of the tenant's working-day calendars
  (kind ``Calendar`` of the catalog). A version is written only when the
  canonical hash of the spec differs from the latest one; a trigger rejects
  every UPDATE and DELETE, since process journals name the version their
  deadlines were computed on.

* ``process_definitions`` — immutable versions of the tenant's processes
  (kind ``Process``, CP-ADR-0074 §1): ``(tenant, key, version)`` is unique,
  the hash is the one of the canonical spec, a trigger rejects every UPDATE
  and DELETE, since instances are pinned to their version. ``governed_by`` —
  the documents the elements name, with a GIN index for ``?governedBy=``.

* ``process_instances`` — cases of a process pinned to their version
  (CP-ADR-0074 §3): ``(tenant, definition_key, instance_key)`` is unique, one
  instance per key; the engine's state beside a readable copy of its data;
  ``refs`` (GIN) routes tasks, approvals, skill calls and child instances back
  to the activity that opened them.

* ``process_timers`` — timers as the engine set them (§8): a partial index on
  ``due_at`` of the pending ones feeds the timer loop.

* ``process_instance_events`` — the instance journal (§5): one row per step,
  input whole, decisions and intents; ``(instance, source_ref)`` is unique so a
  redelivered input is taken once; append-only (trigger).

Downgrade drops every table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8e3f1c6d2a9"
down_revision: str | None = "a7c4e2d9b3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CALENDAR_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_calendar_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'calendars rows are immutable (% rejected)', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""


_PROCESS_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_process_definition_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'process_definitions rows are immutable (% rejected)', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""


_JOURNAL_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_process_journal_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'process_instance_events rows are immutable (% rejected)', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "calendars",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("calendar_hash", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_calendars_version_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_calendars_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_calendars_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_calendars"),
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_calendars_tenant_key_version"),
    )
    op.execute(_CALENDAR_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER calendars_immutable BEFORE UPDATE OR DELETE ON calendars "
        "FOR EACH ROW EXECUTE FUNCTION forbid_calendar_mutation()"
    )

    op.create_table(
        "process_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("definition_hash", sa.Text(), nullable=False),
        sa.Column("identity_agent", sa.Text(), nullable=True),
        sa.Column("expression_profile", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("governed_by", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_process_definitions_version_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_process_definitions_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_process_definitions_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_process_definitions_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_definitions"),
        sa.UniqueConstraint(
            "tenant_id", "key", "version", name="uq_process_definitions_tenant_key_version"
        ),
    )
    op.create_index(
        "ix_process_definitions_governed_by",
        "process_definitions",
        ["governed_by"],
        postgresql_using="gin",
        postgresql_ops={"governed_by": "jsonb_path_ops"},
    )
    op.execute(_PROCESS_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER process_definitions_immutable BEFORE UPDATE OR DELETE"
        " ON process_definitions FOR EACH ROW EXECUTE FUNCTION"
        " forbid_process_definition_mutation()"
    )

    op.create_table(
        "process_instances",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("definition_id", sa.UUID(), nullable=False),
        sa.Column("definition_key", sa.Text(), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("instance_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parent_instance_id", sa.UUID(), nullable=True),
        sa.Column("parent_activity_id", sa.Text(), nullable=True),
        sa.Column("started_by", sa.UUID(), nullable=True),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'suspended', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_process_instances_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_process_instances_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_process_instances_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["definition_id"],
            ["process_definitions.id"],
            name="fk_process_instances_definition_id_process_definitions",
        ),
        sa.ForeignKeyConstraint(
            ["parent_instance_id"],
            ["process_instances.id"],
            name="fk_process_instances_parent_instance_id_process_instances",
        ),
        sa.ForeignKeyConstraint(
            ["started_by"], ["principals.id"], name="fk_process_instances_started_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_instances"),
        sa.UniqueConstraint(
            "tenant_id",
            "definition_key",
            "instance_key",
            name="uq_process_instances_tenant_definition_key_instance_key",
        ),
    )
    op.create_index(
        "ix_process_instances_tenant_status",
        "process_instances",
        ["tenant_id", "status", "definition_key"],
    )
    op.create_index(
        "ix_process_instances_refs", "process_instances", ["refs"], postgresql_using="gin"
    )
    op.create_index("ix_process_instances_parent", "process_instances", ["parent_instance_id"])

    op.create_table(
        "process_timers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("instance_id", sa.UUID(), nullable=False),
        sa.Column("element", sa.Text(), nullable=False),
        sa.Column("timer_kind", sa.Text(), nullable=False),
        sa.Column("due_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("remaining_seconds", sa.Float(), nullable=True),
        sa.Column("reads", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provisional", sa.Boolean(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("fired_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'frozen', 'fired', 'cancelled')",
            name=op.f("ck_process_timers_state_known"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_process_timers_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["process_instances.id"],
            name="fk_process_timers_instance_id_process_instances",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_timers"),
    )
    op.create_index(
        "ix_process_timers_due",
        "process_timers",
        ["due_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index("ix_process_timers_instance", "process_timers", ["instance_id"])

    op.create_table(
        "process_instance_events",
        sa.Column("instance_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=True),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decisions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("intents", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("calendars", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["process_instances.id"],
            name="fk_process_instance_events_instance_id_process_instances",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_process_instance_events_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("instance_id", "seq", name="pk_process_instance_events"),
        sa.UniqueConstraint(
            "instance_id", "source_ref", name="uq_process_instance_events_instance_source"
        ),
    )
    op.execute(_JOURNAL_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER process_instance_events_immutable BEFORE UPDATE OR DELETE"
        " ON process_instance_events FOR EACH ROW EXECUTE FUNCTION"
        " forbid_process_journal_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS process_instance_events_immutable ON process_instance_events"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_process_journal_mutation()")
    op.drop_table("process_instance_events")
    op.drop_table("process_timers")
    op.drop_table("process_instances")
    op.execute("DROP TRIGGER IF EXISTS process_definitions_immutable ON process_definitions")
    op.execute("DROP FUNCTION IF EXISTS forbid_process_definition_mutation()")
    op.drop_table("process_definitions")
    op.execute("DROP TRIGGER IF EXISTS calendars_immutable ON calendars")
    op.execute("DROP FUNCTION IF EXISTS forbid_calendar_mutation()")
    op.drop_table("calendars")
