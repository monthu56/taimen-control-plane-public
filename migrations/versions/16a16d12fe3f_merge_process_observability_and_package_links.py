"""Merge the process-observability head with the head of main (TASK-000895).

Revision ID: 16a16d12fe3f
Revises: a4d9e2c7f3b1, b5d1e7a3c9f4
Create Date: 2026-10-01

Two parallel lines grew from ``e6b3d8f1a2c9``:

* ``a4d9e2c7f3b1`` — the engine revision, step attempts and SLA deadlines of
  processes (CP-ADR-0074 amendment 2026-09-29, CP-ADR-0078), feature
  ``process-observability``;
* ``e3b7d1a9c5f2`` → ``a4d8c2e6f1b3`` → ``a8e3c1f5b920`` → ``b5d1e7a3c9f4`` —
  task text search, the source of an agent revision, runs by principal and
  the package links of every catalog kind (CP-ADR-0074 amendment TASK-000904),
  ``main``.

They touch disjoint tables and columns; the merge has no operations.
"""

from collections.abc import Sequence

revision: str = "16a16d12fe3f"
down_revision: tuple[str, str] = ("a4d9e2c7f3b1", "b5d1e7a3c9f4")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Both parent lines are independent; the merge has no operations."""


def downgrade() -> None:
    """Downgrade separates the graph back into its two parent heads."""
