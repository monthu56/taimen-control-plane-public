"""event journal: iam_actor_id (CP-ADR-0055)

Revision ID: a9c4e2d7f1b3
Revises: c2d8e4f6a1b3
Create Date: 2026-09-12

The journal records the IAM identity of the actor next to the local principal
id: policy-service projects ownership relations on the subject the access
token names. Nullable — legacy API keys and earlier events carry none.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e2d7f1b3"
down_revision: str | None = "c2d8e4f6a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("iam_actor_id", sa.UUID(), nullable=True))
    op.add_column("event_archive", sa.Column("iam_actor_id", sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column("event_archive", "iam_actor_id")
    op.drop_column("events", "iam_actor_id")
