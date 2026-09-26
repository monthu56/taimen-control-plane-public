"""Workspace of an approval about a task (CP-ADR-0068): the task's one, or an
explicitly given ancestor of it — never narrower, never beside it."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.application.commands import approvals
from control_plane.domain.errors import ValidationError

ROOT, CHILD, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture(autouse=True)
def _tree(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ancestors(_session: Any, _tenant: uuid.UUID, workspace_id: uuid.UUID) -> list[Any]:
        return {CHILD: [CHILD, ROOT], ROOT: [ROOT], OTHER: [OTHER]}[workspace_id]

    monkeypatch.setattr(approvals, "workspace_ancestor_ids", ancestors)


def _task(workspace_id: uuid.UUID | None) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), workspace_id=workspace_id)


async def _resolve(task: Any, explicit: uuid.UUID | None) -> uuid.UUID | None:
    ctx: Any = SimpleNamespace(tenant_id=uuid.uuid4())
    return await approvals._task_approval_workspace(None, ctx, task, explicit)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("task_workspace", "explicit", "expected"),
    [
        (CHILD, None, CHILD),
        (CHILD, CHILD, CHILD),
        (CHILD, ROOT, ROOT),
        (None, None, None),
    ],
)
async def test_the_approval_inherits_or_widens_the_tasks_workspace(
    task_workspace: uuid.UUID | None, explicit: uuid.UUID | None, expected: uuid.UUID | None
) -> None:
    assert await _resolve(_task(task_workspace), explicit) == expected


@pytest.mark.parametrize(
    ("task_workspace", "explicit"),
    [(CHILD, OTHER), (ROOT, CHILD), (None, ROOT)],
)
async def test_a_workspace_outside_the_tasks_ancestry_is_refused(
    task_workspace: uuid.UUID | None, explicit: uuid.UUID
) -> None:
    with pytest.raises(ValidationError) as raised:
        await _resolve(_task(task_workspace), explicit)
    assert raised.value.code == "invalid_approval"
