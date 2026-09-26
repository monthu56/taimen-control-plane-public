"""v0.4: reliable event cursor + context adapter consumer state.

1. ``ix_events_tenant_tx_sequence`` — the (tenant_id, tx_id, sequence)
   composite index behind the new replay order. Delivery/pagination is
   ``WHERE tenant_id = :t AND (tx_id, sequence) > (:tx, :seq) AND
   tx_id < pg_snapshot_xmin(...) ORDER BY tx_id, sequence`` — a row
   comparison the planner serves directly from this index.
2. ``event_consumer_cursors`` — durable positions of background journal
   consumers (the context adapter). One row per consumer name; the position
   is the (tx_id, sequence) pair of the last event whose delivery the
   downstream provider confirmed. Advanced only after confirmation
   (at-least-once delivery).

No event rows are modified: ``tx_id`` has been populated by a server
default since the initial v0.1 schema, so the historical journal is already
replayable under the new order.

Operational notes:

* The index builds are NOT ``CONCURRENTLY`` (they run inside Alembic's
  transaction): on a large ``events`` journal they hold a SHARE lock and
  block writes for the build duration — schedule the upgrade accordingly,
  or build the indexes concurrently by hand beforehand (the migration's
  CREATE INDEX will then be fast no-op-equivalent only if names differ —
  prefer a maintenance window).
* ``downgrade`` DROPS ``event_consumer_cursors`` — the adapter's confirmed
  position is lost. After a downgrade/upgrade roundtrip the adapter
  re-delivers the ENTIRE journal; the Memory Service deduplicates
  (at-least-once), but expect proportional provider ingest load. To skip
  the replay, re-seed the cursor row manually before starting the adapter.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2cb05920015d"
down_revision: str | None = "b3d47a1c9e05"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_tenant_tx_sequence",
        "events",
        ["tenant_id", "tx_id", "sequence"],
    )
    # The context adapter consumes the journal across ALL tenants in one
    # global (tx_id, sequence) order — this index serves that scan.
    op.create_index(
        "ix_events_tx_sequence",
        "events",
        ["tx_id", "sequence"],
    )
    op.create_table(
        "event_consumer_cursors",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("tx_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_table("event_consumer_cursors")
    op.drop_index("ix_events_tx_sequence", table_name="events")
    op.drop_index("ix_events_tenant_tx_sequence", table_name="events")
