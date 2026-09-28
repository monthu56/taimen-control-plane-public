"""the agent a work rule acts as (CP-ADR-0063, amendment 2026-09-27, G1)

Revision ID: a7c4e2d9b3f1
Revises: 5d2e8f1a7c63
Create Date: 2026-09-27

* ``work_rules.identity_agent_key`` — key of the registry agent (CP-ADR-0073)
  whose principal the rule evaluates and acts as. ``NULL`` — the rule acts
  with the authority of whoever last enabled or changed it, the behaviour
  before this revision (declarative-cycle C005). A key, not a foreign key: an
  agent row is never deleted and its key is never reused (retirement), and
  the rule refers to the description, not to a principal.

Downgrade is lossy: rules forget their identity and act as their enabler.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e2d9b3f1"
down_revision: str | None = "5d2e8f1a7c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("work_rules", sa.Column("identity_agent_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_rules", "identity_agent_key")
