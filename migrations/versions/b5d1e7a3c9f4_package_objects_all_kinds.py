"""package_objects for every catalog kind of the core (CP-ADR-0074 §11, amendment TASK-000904)

Revision ID: b5d1e7a3c9f4
Revises: a8e3c1f5b920
Create Date: 2026-09-29

``package_objects`` becomes the link of a catalog object to the package that
installed it, for every kind the core holds, not only the kinds it plans:

* the kind check admits ``ArtifactType``, ``TaskType``, ``ProjectTemplate``,
  ``WorkspaceType``, ``Role``, ``Capability``, ``Skill``, ``WorkRule`` and
  ``Agent`` besides ``Process`` and ``Calendar``;
* ``package_version`` — the version of the package (``package.yaml →
  spec.version``); rows applied before this revision have none;
* ``plan_hash`` (the hash of the installation), ``version``, ``spec`` and
  ``spec_hash`` become nullable: the installer's links (``POST
  /packages:record``) carry no plan and no spec; a planned kind keeps all
  four (``ck_package_objects_planned_spec``);
* ``ix_package_objects_tenant_package`` — ``?package=<key>`` of the lists;
* agents whose revisions name a package get the link of the newest such
  revision.

Downgrade forgets the links of the kinds the core does not plan and the
package versions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d1e7a3c9f4"
down_revision: str | None = "a8e3c1f5b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "ArtifactType",
    "TaskType",
    "ProjectTemplate",
    "WorkspaceType",
    "Role",
    "Capability",
    "Skill",
    "WorkRule",
    "Agent",
    "Process",
    "Calendar",
)
PLANNED = "kind NOT IN ('Process', 'Calendar')"


def _in(kinds: Sequence[str]) -> str:
    return "kind IN (" + ", ".join(f"'{kind}'" for kind in kinds) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_package_objects_kind_known"), "package_objects", type_="check")
    op.create_check_constraint(op.f("ck_package_objects_kind_known"), "package_objects", _in(KINDS))
    op.add_column("package_objects", sa.Column("package_version", sa.Text(), nullable=True))
    for column in ("plan_hash", "version", "spec", "spec_hash"):
        op.alter_column("package_objects", column, nullable=True)
    op.create_check_constraint(
        op.f("ck_package_objects_planned_spec"),
        "package_objects",
        f"{PLANNED} OR (plan_hash IS NOT NULL AND version IS NOT NULL"
        " AND spec IS NOT NULL AND spec_hash IS NOT NULL)",
    )
    op.create_index(
        "ix_package_objects_tenant_package",
        "package_objects",
        ["tenant_id", "package_key", "kind"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO package_objects
            (id, tenant_id, kind, key, package_key, package_version, applied_by, applied_at)
        SELECT DISTINCT ON (a.id)
            gen_random_uuid(), a.tenant_id, 'Agent', a.key, r.source_package_key,
            r.source_package_version, r.created_by, r.created_at
        FROM agents a
        JOIN agent_revisions r ON r.agent_id = a.id
        WHERE r.source_kind = 'package'
        ORDER BY a.id, r.revision DESC
        ON CONFLICT (tenant_id, kind, key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM package_objects WHERE {PLANNED}")
    op.drop_index("ix_package_objects_tenant_package", table_name="package_objects")
    op.drop_constraint(op.f("ck_package_objects_planned_spec"), "package_objects", type_="check")
    for column in ("plan_hash", "version", "spec", "spec_hash"):
        op.alter_column("package_objects", column, nullable=False)
    op.drop_column("package_objects", "package_version")
    op.drop_constraint(op.f("ck_package_objects_kind_known"), "package_objects", type_="check")
    op.create_check_constraint(
        op.f("ck_package_objects_kind_known"), "package_objects", _in(("Process", "Calendar"))
    )
