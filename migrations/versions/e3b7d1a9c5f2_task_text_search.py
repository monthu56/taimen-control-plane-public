"""text search over the task list: pg_trgm GIN indexes (CP-ADR-0049, TASK-000866)

Revision ID: e3b7d1a9c5f2
Revises: e6b3d8f1a2c9
Create Date: 2026-09-29

* ``pg_trgm`` — trigram operator classes. A trusted extension since
  PostgreSQL 13: the database owner may create it without superuser.
* ``ix_tasks_title_trgm``, ``ix_tasks_description_trgm``,
  ``ix_tasks_public_id_trgm`` — GIN ``gin_trgm_ops`` indexes that serve
  ``GET /tasks?q=``: every term is ``title ILIKE '%t%' OR description ILIKE
  '%t%' OR public_id ILIKE '%t%'``, a BitmapOr over the three indexes.

Downgrade drops the indexes and leaves the extension: other objects of the
database may already rely on it, and an unused extension costs nothing.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3b7d1a9c5f2"
down_revision: str | None = "e6b3d8f1a2c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("title", "description", "public_id")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for column in _COLUMNS:
        op.create_index(
            f"ix_tasks_{column}_trgm",
            "tasks",
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )


def downgrade() -> None:
    for column in _COLUMNS:
        op.drop_index(f"ix_tasks_{column}_trgm", table_name="tasks")
