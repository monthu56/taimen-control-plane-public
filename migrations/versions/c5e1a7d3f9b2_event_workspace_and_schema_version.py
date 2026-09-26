"""event journal: workspace of the event and version of its data (CP-ADR-0068)

Revision ID: c5e1a7d3f9b2
Revises: b8d3f1a6c2e9
Create Date: 2026-09-25

Schema (``events`` and ``event_archive`` alike, the archive copies rows 1:1):

* ``workspace_id`` — the workspace the event belongs to, resolved by the
  writer from the event's entity (a task's, an approval's, a run's task's
  workspace...). ``NULL`` for tenant-level events and for every event written
  before this revision: the journal is append-only, history is not rewritten,
  so a ``workspaceId`` filter sees events from this revision on.
* ``schema_version`` — the version of the ``payload`` schema in the event
  catalog (``domain/event_catalog.py``). Existing rows get ``1``: every type
  was at version 1 until this revision (``approval.*`` moves to 2 with it).

Both columns are added without a rewrite (a nullable column and a constant
default), so the append-only trigger is not involved. No index: the filter
narrows the ordered replay scan the same way ``entityType`` does.

Downgrade drops both columns: the workspace of new events and the version
marks are lost, payloads stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e1a7d3f9b2"
down_revision: str | None = "b8d3f1a6c2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("events", "event_archive")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("workspace_id", sa.UUID(), nullable=True))
        op.add_column(
            table,
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "schema_version")
        op.drop_column(table, "workspace_id")
