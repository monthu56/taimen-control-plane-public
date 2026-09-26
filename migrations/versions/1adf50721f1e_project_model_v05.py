"""v0.5: Project Model, per-tenant delivery isolation, journal retention.

Revision ID: 1adf50721f1e
Revises: 2cb05920015d

Five independent groups of change:

1. **Workspace Types** (ADR-0029) — ``workspace_types`` plus ``workspaces.type_id``
   / ``workspaces.custom_fields``. A system fallback type (``key = 'generic'``,
   ``allowed_child_types = ["*"]``) is created for every existing tenant and
   backfilled onto every existing workspace, so a v0.4 tree upgrades with no
   manual repair and no behaviour change.

2. **Project Model** (ADR-0030..0032) — ``project_templates``,
   ``project_profiles``, ``project_config_revisions``, ``external_references``.
   Immutability is enforced by database triggers, not only by the application:
   a template version and a config revision cannot be edited in place, and an
   external reference's identity columns cannot be rewritten.

3. **Trace id** (ADR-0039) — nullable ``events.trace_run_id``; historical
   events legitimately carry none.

4. **Per-tenant adapter cursors** (ADR-0036) — the primary key of
   ``event_consumer_cursors`` becomes ``(name, tenant_id)``. The conversion is
   replay-safe by construction: the old global position G means "everything at
   or below G was confirmed", so seeding every existing tenant at G asserts
   exactly the same thing — no re-delivery of the whole journal, no gap.
   Tenants created later start at the origin, which is correct by definition.

5. **Journal retention** (ADR-0038) — ``event_archive`` (cold copy of the
   journal) and the ``event_journal_floor`` singleton describing what can still
   be served. Both start empty/at origin, so nothing changes until an operator
   runs an archive.

Operational notes:

* Index builds are NOT ``CONCURRENTLY`` (Alembic runs DDL in a transaction):
  on a large journal ``event_archive``'s indexes are cheap (empty table), but
  ``workspaces``' backfill UPDATE touches every row — schedule a window.
* ``downgrade`` restores archived events back into ``events`` (with
  ``OVERRIDING SYSTEM VALUE`` and an identity resync) and folds per-tenant
  cursors into one global row at the MINIMUM position across tenants:
  conservative — some events are re-delivered, none are lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1adf50721f1e"
down_revision: str | None = "2cb05920015d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_TEMPLATE_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_project_template_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'project_templates rows are immutable (DELETE rejected)';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.key IS DISTINCT FROM OLD.key
        OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.display_name IS DISTINCT FROM OLD.display_name
        OR NEW.description IS DISTINCT FROM OLD.description
        OR NEW.field_schema IS DISTINCT FROM OLD.field_schema
        OR NEW.lifecycle_schema IS DISTINCT FROM OLD.lifecycle_schema
        OR NEW.default_config IS DISTINCT FROM OLD.default_config
        OR NEW.default_views IS DISTINCT FROM OLD.default_views
        OR NEW.governance_schema IS DISTINCT FROM OLD.governance_schema
        OR NEW.memory_defaults IS DISTINCT FROM OLD.memory_defaults
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'project_templates content is immutable; create a new version';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
        AND NOT (OLD.status = 'active' AND NEW.status = 'deprecated')
    THEN
        RAISE EXCEPTION 'project_templates status may only move active -> deprecated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_REVISION_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_project_config_revision_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'project_config_revisions is append-only (DELETE rejected)';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.revision IS DISTINCT FROM OLD.revision
        OR NEW.config IS DISTINCT FROM OLD.config
        OR NEW.validation IS DISTINCT FROM OLD.validation
        OR NEW.comment IS DISTINCT FROM OLD.comment
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'project_config_revisions rows are immutable; create a new revision';
    END IF;
    IF NEW.activated_at IS NULL AND OLD.activated_at IS NOT NULL THEN
        RAISE EXCEPTION 'activated_at cannot be cleared';
    END IF;
    IF OLD.activated_at IS NOT NULL AND NEW.activated_at < OLD.activated_at THEN
        RAISE EXCEPTION 'activated_at cannot move backwards';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_EXTERNAL_REF_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION forbid_external_reference_identity_change() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.entity_type IS DISTINCT FROM OLD.entity_type
        OR NEW.entity_id IS DISTINCT FROM OLD.entity_id
        OR NEW.external_system IS DISTINCT FROM OLD.external_system
        OR NEW.external_type IS DISTINCT FROM OLD.external_type
        OR NEW.external_id IS DISTINCT FROM OLD.external_id
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'external_references identity is immutable; only metadata may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_ARCHIVING_AWARE_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION forbid_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- v0.5 retention (ADR-0038): the ONLY sanctioned way to remove a row from
    -- `events` is the audited archive command, which sets this transaction-local
    -- flag while moving rows into `event_archive`. UPDATE and TRUNCATE stay
    -- forbidden unconditionally, so the journal is still append-only.
    IF TG_OP = 'DELETE'
       AND coalesce(current_setting('cp.journal_archiving', true), 'off') = 'on'
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'events are append-only (% on events is forbidden)', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;
"""

_STRICT_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION forbid_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'events are append-only (% on events is forbidden)', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;
"""


def upgrade() -> None:
    # --- 1. Workspace types ---------------------------------------------------
    op.create_table(
        "workspace_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("field_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allowed_child_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name=op.f("ck_workspace_types_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_workspace_types_version_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_workspace_types_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_types"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workspace_types_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_workspace_types_tenant_key"),
    )
    op.create_index(
        "uq_workspace_types_one_system",
        "workspace_types",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_system"),
    )
    op.create_index(
        "ix_workspace_types_tenant_created",
        "workspace_types",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )

    # One system fallback type per existing tenant; it allows any child, so the
    # v0.4 tree stays valid under the new parent/child rule.
    op.execute(
        """
        INSERT INTO workspace_types (
            id, tenant_id, key, display_name, description, field_schema,
            allowed_child_types, is_system, status, version, created_at, updated_at
        )
        SELECT gen_random_uuid(), t.id, 'generic', 'Generic Workspace',
               'System default workspace type (v0.5 backfill).',
               '{}'::jsonb, '["*"]'::jsonb, true, 'active', 1, now(), now()
        FROM tenants t
        """
    )

    op.add_column("workspaces", sa.Column("type_id", sa.UUID(), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        """
        UPDATE workspaces w
           SET type_id = wt.id
          FROM workspace_types wt
         WHERE wt.tenant_id = w.tenant_id AND wt.is_system
        """
    )
    op.alter_column("workspaces", "type_id", nullable=False)
    op.create_foreign_key(
        "fk_workspaces_type_id_workspace_types",
        "workspaces",
        "workspace_types",
        ["type_id"],
        ["id"],
    )

    # --- 2. Project model -----------------------------------------------------
    op.create_table(
        "project_templates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("field_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lifecycle_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("default_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("default_views", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("governance_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("memory_defaults", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated')", name=op.f("ck_project_templates_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_project_templates_version_positive")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_project_templates_created_by_principals"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_project_templates_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_templates"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_templates_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_project_templates_key_version"),
    )
    op.create_index(
        "ix_project_templates_tenant_created",
        "project_templates",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_project_templates_tenant_key",
        "project_templates",
        ["tenant_id", "key", "version"],
        unique=False,
    )

    op.create_table(
        "project_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column("status_key", sa.Text(), nullable=False),
        sa.Column("system_status_category", sa.Text(), nullable=False),
        sa.Column("owner_principal_id", sa.UUID(), nullable=True),
        sa.Column("start_date", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("target_date", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("custom_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active_config_revision_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("archived_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name=op.f("ck_project_profiles_status")
        ),
        sa.CheckConstraint(
            "system_status_category IN ('planned', 'active', 'paused', "
            "'terminal_success', 'terminal_cancelled')",
            name=op.f("ck_project_profiles_system_status_category"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_project_profiles_version_positive")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_project_profiles_created_by_principals"
        ),
        sa.ForeignKeyConstraint(
            ["owner_principal_id"],
            ["principals.id"],
            name="fk_project_profiles_owner_principal_id_principals",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["project_templates.tenant_id", "project_templates.id"],
            name="fk_project_profiles_template",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_project_profiles_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_project_profiles_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_profiles"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_profiles_tenant_id_id"),
        # The whole concurrency story of project creation.
        sa.UniqueConstraint("workspace_id", name="uq_project_profiles_workspace"),
    )
    op.create_index(
        "ix_project_profiles_template", "project_profiles", ["template_id"], unique=False
    )
    op.create_index(
        "ix_project_profiles_tenant_created",
        "project_profiles",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_project_profiles_tenant_status",
        "project_profiles",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "project_config_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("activated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "revision >= 1", name=op.f("ck_project_config_revisions_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
            name="fk_project_config_revisions_created_by_principals",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["project_profiles.tenant_id", "project_profiles.id"],
            name="fk_project_config_revisions_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_project_config_revisions_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_config_revisions"),
        sa.UniqueConstraint("project_id", "revision", name="uq_project_config_revisions_revision"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_config_revisions_tenant_id_id"),
    )
    op.create_index(
        "ix_project_config_revisions_project",
        "project_config_revisions",
        ["project_id", "revision"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_project_profiles_active_revision",
        "project_profiles",
        "project_config_revisions",
        ["active_config_revision_id"],
        ["id"],
    )

    op.create_table(
        "external_references",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("external_system", sa.Text(), nullable=False),
        sa.Column("external_type", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_external_references_version_positive")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_external_references_created_by_principals"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_external_references_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_references"),
        sa.UniqueConstraint(
            "tenant_id",
            "external_system",
            "external_type",
            "external_id",
            name="uq_external_references_external_key",
        ),
    )
    op.create_index(
        "ix_external_references_entity",
        "external_references",
        ["tenant_id", "entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_references_tenant_created",
        "external_references",
        ["tenant_id", "created_at", "id"],
        unique=False,
    )

    # Immutability enforced by the database, not only by the command layer.
    op.execute(_TEMPLATE_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER project_templates_immutable BEFORE UPDATE OR DELETE ON project_templates "
        "FOR EACH ROW EXECUTE FUNCTION forbid_project_template_mutation()"
    )
    op.execute(_REVISION_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER project_config_revisions_append_only "
        "BEFORE UPDATE OR DELETE ON project_config_revisions "
        "FOR EACH ROW EXECUTE FUNCTION forbid_project_config_revision_mutation()"
    )
    op.execute(_EXTERNAL_REF_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER external_references_identity_immutable"
        " BEFORE UPDATE ON external_references"
        " FOR EACH ROW EXECUTE FUNCTION forbid_external_reference_identity_change()"
    )

    # --- 3. Trace id on the journal -------------------------------------------
    op.add_column("events", sa.Column("trace_run_id", sa.Text(), nullable=True))

    # --- 4. Per-tenant consumer cursors ---------------------------------------
    op.add_column("event_consumer_cursors", sa.Column("tenant_id", sa.UUID(), nullable=True))
    op.add_column(
        "event_consumer_cursors",
        sa.Column("parked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("event_consumer_cursors", sa.Column("parked_reason", sa.Text(), nullable=True))
    op.add_column("event_consumer_cursors", sa.Column("parked_event_id", sa.UUID(), nullable=True))
    op.add_column(
        "event_consumer_cursors",
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "event_consumer_cursors",
        sa.Column("next_attempt_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.drop_constraint("pk_event_consumer_cursors", "event_consumer_cursors", type_="primary")
    # Fan the single global position out to one row per tenant. Same assertion,
    # per tenant: "everything at or below G was confirmed for this tenant".
    op.execute(
        """
        INSERT INTO event_consumer_cursors (
            name, tenant_id, tx_id, sequence, updated_at, metadata, failure_count
        )
        SELECT c.name, t.id, c.tx_id, c.sequence, c.updated_at, c.metadata, 0
        FROM event_consumer_cursors c CROSS JOIN tenants t
        WHERE c.tenant_id IS NULL
        """
    )
    op.execute("DELETE FROM event_consumer_cursors WHERE tenant_id IS NULL")
    op.alter_column("event_consumer_cursors", "tenant_id", nullable=False)
    op.create_primary_key(
        "pk_event_consumer_cursors", "event_consumer_cursors", ["name", "tenant_id"]
    )
    op.create_foreign_key(
        "fk_event_consumer_cursors_tenant_id_tenants",
        "event_consumer_cursors",
        "tenants",
        ["tenant_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_event_consumer_cursors_failure_count_nonnegative"),
        "event_consumer_cursors",
        "failure_count >= 0",
    )
    op.create_index(
        "ix_event_consumer_cursors_name",
        "event_consumer_cursors",
        ["name", "updated_at"],
        unique=False,
    )

    # --- 5. Journal archive and floor -----------------------------------------
    op.create_table(
        "event_archive",
        sa.Column("sequence", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("tx_id", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("causation_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("trace_run_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("archived_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_event_archive_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("sequence", name="pk_event_archive"),
        sa.UniqueConstraint("id", name="uq_event_archive_id"),
    )
    op.create_index(
        "ix_event_archive_entity",
        "event_archive",
        ["tenant_id", "entity_type", "entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_event_archive_tenant_tx_sequence",
        "event_archive",
        ["tenant_id", "tx_id", "sequence"],
        unique=False,
    )
    op.create_table(
        "event_journal_floor",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("journal_tx_id", sa.BigInteger(), nullable=False),
        sa.Column("journal_sequence", sa.BigInteger(), nullable=False),
        sa.Column("archive_tx_id", sa.BigInteger(), nullable=False),
        sa.Column("archive_sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_event_journal_floor_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_event_journal_floor"),
    )
    op.execute(_ARCHIVING_AWARE_APPEND_ONLY_FN)
    # One floor row per existing tenant; new tenants get one lazily.
    op.execute(
        "INSERT INTO event_journal_floor (tenant_id, journal_tx_id, journal_sequence,"
        " archive_tx_id, archive_sequence, updated_at)"
        " SELECT t.id, 0, 0, 0, 0, now() FROM tenants t"
    )


def downgrade() -> None:
    # --- 5. Journal archive and floor -----------------------------------------
    # Restore archived events into the hot journal: a downgrade must not be the
    # operation that loses history. Identity is resynced afterwards.
    op.execute(
        """
        INSERT INTO events (
            sequence, tx_id, id, tenant_id, event_type, entity_type, entity_id,
            actor_id, session_id, correlation_id, causation_id, request_id,
            payload, occurred_at
        )
        OVERRIDING SYSTEM VALUE
        SELECT sequence, tx_id, id, tenant_id, event_type, entity_type, entity_id,
               actor_id, session_id, correlation_id, causation_id, request_id,
               payload, occurred_at
        FROM event_archive
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('events', 'sequence'), "
        "GREATEST((SELECT COALESCE(max(sequence), 1) FROM events), 1), true)"
    )
    op.execute(_STRICT_APPEND_ONLY_FN)
    op.drop_table("event_journal_floor")
    op.drop_index("ix_event_archive_tenant_tx_sequence", table_name="event_archive")
    op.drop_index("ix_event_archive_entity", table_name="event_archive")
    op.drop_table("event_archive")

    # --- 4. Fold per-tenant cursors back into one global row ------------------
    op.drop_index("ix_event_consumer_cursors_name", table_name="event_consumer_cursors")
    op.drop_constraint(
        op.f("ck_event_consumer_cursors_failure_count_nonnegative"),
        "event_consumer_cursors",
        type_="check",
    )
    op.drop_constraint(
        "fk_event_consumer_cursors_tenant_id_tenants", "event_consumer_cursors", type_="foreignkey"
    )
    op.drop_constraint("pk_event_consumer_cursors", "event_consumer_cursors", type_="primary")
    # Keep the MINIMUM position per consumer: the global cursor must not claim
    # more than the least-advanced tenant. Re-delivery is safe, skipping is not.
    op.execute(
        """
        DELETE FROM event_consumer_cursors c
        USING event_consumer_cursors o
        WHERE c.name = o.name AND (o.tx_id, o.sequence) < (c.tx_id, c.sequence)
        """
    )
    op.execute(
        """
        DELETE FROM event_consumer_cursors c
        USING event_consumer_cursors o
        WHERE c.name = o.name AND c.ctid > o.ctid
        """
    )
    op.drop_column("event_consumer_cursors", "next_attempt_at")
    op.drop_column("event_consumer_cursors", "failure_count")
    op.drop_column("event_consumer_cursors", "parked_event_id")
    op.drop_column("event_consumer_cursors", "parked_reason")
    op.drop_column("event_consumer_cursors", "parked_at")
    op.drop_column("event_consumer_cursors", "tenant_id")
    op.create_primary_key("pk_event_consumer_cursors", "event_consumer_cursors", ["name"])

    # --- 3. Trace id ----------------------------------------------------------
    op.drop_column("events", "trace_run_id")

    # --- 2. Project model -----------------------------------------------------
    op.execute(
        "DROP TRIGGER IF EXISTS external_references_identity_immutable ON external_references"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_external_reference_identity_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS project_config_revisions_append_only ON project_config_revisions"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_project_config_revision_mutation()")
    op.execute("DROP TRIGGER IF EXISTS project_templates_immutable ON project_templates")
    op.execute("DROP FUNCTION IF EXISTS forbid_project_template_mutation()")
    op.drop_index("ix_external_references_tenant_created", table_name="external_references")
    op.drop_index("ix_external_references_entity", table_name="external_references")
    op.drop_table("external_references")
    op.drop_constraint(
        "fk_project_profiles_active_revision", "project_profiles", type_="foreignkey"
    )
    op.drop_index("ix_project_config_revisions_project", table_name="project_config_revisions")
    op.drop_table("project_config_revisions")
    op.drop_index("ix_project_profiles_tenant_status", table_name="project_profiles")
    op.drop_index("ix_project_profiles_tenant_created", table_name="project_profiles")
    op.drop_index("ix_project_profiles_template", table_name="project_profiles")
    op.drop_table("project_profiles")
    op.drop_index("ix_project_templates_tenant_key", table_name="project_templates")
    op.drop_index("ix_project_templates_tenant_created", table_name="project_templates")
    op.drop_table("project_templates")

    # --- 1. Workspace types ---------------------------------------------------
    op.drop_constraint("fk_workspaces_type_id_workspace_types", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "custom_fields")
    op.drop_column("workspaces", "type_id")
    op.drop_index("ix_workspace_types_tenant_created", table_name="workspace_types")
    op.drop_index("uq_workspace_types_one_system", table_name="workspace_types")
    op.drop_table("workspace_types")
