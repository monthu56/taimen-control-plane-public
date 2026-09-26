"""v0.8: work item custom fields, planned dates and their indexes (ADR-0049).

Three additive columns on ``tasks`` and nothing else:

1. ``custom_fields`` (JSONB, ``{}``) — validated at write time against the
   ``field_schema`` of the task's type, which ``c8a51d70b394`` introduced empty
   and applied to nothing. An empty schema accepts everything, so existing
   types keep behaving exactly as before.

2. ``start_date`` / ``due_date`` (timestamptz, NULL) — typed columns, not keys
   inside the document, because they need indexes, ordering and a timeline. A
   CHECK keeps the interval from running backwards; it is trivially true for
   every existing row, both being NULL.

3. Indexes for the filters this revision unlocks: ``(tenant_id, owner_id)``,
   and partial ``(tenant_id, due_date, id)`` / ``(tenant_id, start_date, id)``
   over the rows that actually carry a date. The ``id`` tail is not decoration:
   date-ordered pagination compares the pair, so the index has to carry it.

Operational notes:

* every column is added NULL-able or with a server default, so the ADD COLUMNs
  do not rewrite the table; the index builds are not ``CONCURRENTLY`` (Alembic
  keeps DDL in a transaction) and take a brief lock on ``tasks``;
* downgrade is lossy in the ordinary way — it drops the three columns, so
  custom fields and planned dates are gone. Nothing else depends on them, so
  the rollback is otherwise clean, unlike ``c8a51d70b394``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c7e94b2f60"
down_revision: str | None = "c8a51d70b394"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "tasks", sa.Column("start_date", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "tasks", sa.Column("due_date", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )

    op.create_check_constraint(
        op.f("ck_tasks_planned_dates_ordered"),
        "tasks",
        "start_date IS NULL OR due_date IS NULL OR start_date <= due_date",
    )

    op.create_index("ix_tasks_tenant_owner", "tasks", ["tenant_id", "owner_id"])
    op.create_index(
        "ix_tasks_tenant_due",
        "tasks",
        ["tenant_id", "due_date", "id"],
        postgresql_where=sa.text("due_date IS NOT NULL"),
    )
    op.create_index(
        "ix_tasks_tenant_start",
        "tasks",
        ["tenant_id", "start_date", "id"],
        postgresql_where=sa.text("start_date IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_tenant_start", table_name="tasks")
    op.drop_index("ix_tasks_tenant_due", table_name="tasks")
    op.drop_index("ix_tasks_tenant_owner", table_name="tasks")
    op.drop_constraint(op.f("ck_tasks_planned_dates_ordered"), "tasks", type_="check")
    op.drop_column("tasks", "due_date")
    op.drop_column("tasks", "start_date")
    op.drop_column("tasks", "custom_fields")
