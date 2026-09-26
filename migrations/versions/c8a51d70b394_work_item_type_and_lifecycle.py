"""v0.8: work item type registry and configurable task lifecycle (ADR-0048).

Three changes, applied in one revision because a type without its lifecycle is
half an entity:

1. ``task_types`` — a tenant-scoped, versioned registry shaped exactly like
   ``project_templates``, with the same database-enforced immutability: a
   version never changes after INSERT except ``status: active -> deprecated``.

2. ``tasks.type_id`` and ``tasks.system_status_category``. The status column
   stops being a six-value enumeration and becomes the tenant's own key; the
   category next to it is the only thing core branches on.

3. Data. Every tenant gets a system task type (``key = 'task'``, version 1)
   carrying the six pre-v0.8 statuses and a transition graph that is complete
   over the four non-terminal ones, so no existing transition becomes illegal.
   Every existing task is backfilled with that type and with the category of
   its current status. **Statuses are not rewritten and claimability does not
   change**: pre-v0.8 "claimable" meant "not done and not cancelled", which is
   exactly "category is not terminal" under this mapping.

Operational notes:

* the backfill UPDATE touches every row of ``tasks``, and indexes are not
  built ``CONCURRENTLY`` (Alembic keeps DDL in a transaction) — schedule a
  window, same as for ``1adf50721f1e``;
* **downgrade is lossy and is an emergency path, not a routine rollback.** The
  six-value CHECK cannot accept a status a tenant invented after the upgrade
  (``review``, ``answered``, …), so downgrade collapses every unknown key onto
  the legacy key of its category: ``backlog -> backlog``, ``active -> todo``,
  ``blocked -> blocked``, ``terminal_success -> done``,
  ``terminal_cancelled -> cancelled``. The original keys are gone. Task
  version and history are untouched, so the loss is visible in the journal.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from control_plane.domain.work_item import (
    LEGACY_CATEGORY_STATUSES,
    LEGACY_STATUS_CATEGORIES,
    SYSTEM_TASK_LIFECYCLE,
    SYSTEM_TASK_TYPE_DISPLAY_NAME,
    SYSTEM_TASK_TYPE_KEY,
)

revision: str = "c8a51d70b394"
down_revision: str | None = "f5b91c3e7a24"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


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

# The system type is created BY a principal, and ``created_by`` is a FK. Any
# principal of the tenant would do; the oldest one is deterministic and always
# exists (bootstrap creates the admin before anything else can create a task).
_SEED_SYSTEM_TYPE = """
INSERT INTO task_types (
    id, tenant_id, key, version, display_name, description,
    field_schema, lifecycle_schema, status, created_by, created_at, updated_at
)
SELECT gen_random_uuid(), t.id, :key, 1, :display_name,
       'System work item type (v0.8 backfill).',
       '{}'::jsonb, CAST(:lifecycle AS jsonb), 'active',
       (SELECT p.id FROM principals p
         WHERE p.tenant_id = t.id
         ORDER BY p.created_at, p.id
         LIMIT 1),
       now(), now()
  FROM tenants t
 WHERE EXISTS (SELECT 1 FROM principals p WHERE p.tenant_id = t.id)
"""


def upgrade() -> None:
    # --- 1. The registry ------------------------------------------------------
    op.create_table(
        "task_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "field_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "lifecycle_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'deprecated')", name=op.f("ck_task_types_status")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_task_types_version_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_task_types_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["principals.id"], name="fk_task_types_created_by_principals"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_types"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_task_types_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_task_types_key_version"),
    )
    op.create_index("ix_task_types_tenant_key", "task_types", ["tenant_id", "key", "version"])
    op.create_index("ix_task_types_tenant_created", "task_types", ["tenant_id", "created_at", "id"])
    op.execute(_TASK_TYPE_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER task_types_immutable BEFORE UPDATE OR DELETE ON task_types "
        "FOR EACH ROW EXECUTE FUNCTION forbid_task_type_mutation()"
    )

    # --- 2. The system type for every existing tenant -------------------------
    op.execute(
        sa.text(_SEED_SYSTEM_TYPE).bindparams(
            key=SYSTEM_TASK_TYPE_KEY,
            display_name=SYSTEM_TASK_TYPE_DISPLAY_NAME,
            lifecycle=json.dumps(SYSTEM_TASK_LIFECYCLE),
        )
    )

    # --- 3. Task columns, backfilled before they become NOT NULL --------------
    op.add_column("tasks", sa.Column("type_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("system_status_category", sa.Text(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE tasks t
               SET type_id = tt.id
              FROM task_types tt
             WHERE tt.tenant_id = t.tenant_id
               AND tt.key = :key
               AND tt.version = 1
            """
        ).bindparams(key=SYSTEM_TASK_TYPE_KEY)
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks
               SET system_status_category = CASE status
                   {cases}
                   ELSE 'active'
               END
            """.format(
                cases="\n".join(
                    f"WHEN '{status}' THEN '{category}'"
                    for status, category in LEGACY_STATUS_CATEGORIES.items()
                )
            )
        )
    )

    op.alter_column("tasks", "type_id", nullable=False)
    op.alter_column("tasks", "system_status_category", nullable=False)
    op.create_foreign_key(
        "fk_tasks_type",
        "tasks",
        "task_types",
        ["tenant_id", "type_id"],
        ["tenant_id", "id"],
    )

    # --- 4. The status vocabulary leaves the database -------------------------
    op.drop_constraint(op.f("ck_tasks_status"), "tasks", type_="check")
    # op.f() marks a name as final: alembic would otherwise re-apply the
    # metadata naming convention and produce ck_tasks_ck_tasks_status.
    op.create_check_constraint(
        op.f("ck_tasks_status"), "tasks", "char_length(status) BETWEEN 1 AND 64"
    )
    op.create_check_constraint(
        op.f("ck_tasks_system_status_category"),
        "tasks",
        "system_status_category IN ('backlog', 'active', 'blocked',"
        " 'terminal_success', 'terminal_cancelled')",
    )
    op.create_index("ix_tasks_tenant_category", "tasks", ["tenant_id", "system_status_category"])
    op.create_index("ix_tasks_type", "tasks", ["type_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_type", table_name="tasks")
    op.drop_index("ix_tasks_tenant_category", table_name="tasks")
    op.drop_constraint(op.f("ck_tasks_system_status_category"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_status"), "tasks", type_="check")

    # Lossy on purpose (see the module docstring): a tenant-authored key has no
    # representation in the six-value CHECK, so it collapses onto the legacy
    # key of its category.
    op.execute(
        sa.text(
            """
            UPDATE tasks
               SET status = CASE system_status_category
                   {cases}
                   ELSE 'todo'
               END
             WHERE status NOT IN ({legacy})
            """.format(
                cases="\n".join(
                    f"WHEN '{category}' THEN '{status}'"
                    for category, status in LEGACY_CATEGORY_STATUSES.items()
                ),
                legacy=", ".join(f"'{status}'" for status in LEGACY_STATUS_CATEGORIES),
            )
        )
    )
    op.create_check_constraint(
        op.f("ck_tasks_status"),
        "tasks",
        "status IN ('backlog', 'todo', 'in_progress', 'blocked', 'done', 'cancelled')",
    )

    op.drop_constraint(op.f("fk_tasks_type"), "tasks", type_="foreignkey")
    op.drop_column("tasks", "system_status_category")
    op.drop_column("tasks", "type_id")

    op.execute("DROP TRIGGER IF EXISTS task_types_immutable ON task_types")
    op.execute("DROP FUNCTION IF EXISTS forbid_task_type_mutation()")
    op.drop_index("ix_task_types_tenant_created", table_name="task_types")
    op.drop_index("ix_task_types_tenant_key", table_name="task_types")
    op.drop_table("task_types")
