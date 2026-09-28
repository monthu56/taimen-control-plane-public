"""K012: a skill executor reads a task's artifact (CP-ADR-0072, amendment
2026-09-28, company-knowledge; FR-005, FR-007).

The executor holds ``skills.execute`` but no ``tasks.read``; ``artifacts.read``
granted on the workspace of the artifact's task lets it read the record and
the bytes. Local authorization is flat, so the scoped PDP binding is
simulated at the one seam the read uses, as in ``test_artifact_content``.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from control_plane.application.commands import artifacts as artifact_commands
from control_plane.domain.errors import AuthorizationError
from control_plane.infrastructure.content_store import InMemoryContentStore
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)
from tests.integration.test_artifact_content import events_of, stored_artifact

DATA = b"%PDF-1.7 import plan"
# A skill executor's key: transport identity, no tasks.read (ADR-0056 §5).
EXECUTOR = ["skills.execute", "sessions.open", "artifacts.read"]


@pytest.fixture
def store(app: FastAPI) -> InMemoryContentStore:
    content_store = InMemoryContentStore()
    app.state.content_store = content_store
    return content_store


@pytest.fixture
async def boot(client: httpx.AsyncClient, store: InMemoryContentStore) -> dict[str, Any]:
    body = await do_bootstrap(client)
    admin = body["apiKey"]["key"]
    _, writer = await create_agent_with_key(
        client, admin, name="writer", permissions=ORG_AGENT_PERMISSIONS
    )
    executor, executor_key = await create_agent_with_key(
        client, admin, name="executor", permissions=EXECUTOR
    )
    docs = await create_workspace(client, admin, "docs")
    other_ws = await create_workspace(client, admin, "other")
    task = await create_task(client, admin, title="Import plan", workspaceId=docs["id"])
    artifact = await stored_artifact(client, writer, task["id"], DATA)
    return {
        "admin": admin,
        "executor": executor_key,
        "executorId": executor["id"],
        "docs": docs["id"],
        "other": other_ws["id"],
        "task": task["id"],
        "artifact": artifact["id"],
    }


def pdp_binding(monkeypatch: pytest.MonkeyPatch, grants: dict[str, set[str]]) -> list[str]:
    """Simulate a PDP for the principals in ``grants``.

    ``artifacts.read`` and ``tasks.read`` are allowed only on the resource
    keys listed for the principal (a scoped binding; nobody here holds
    ``tasks.read`` on the task). Any other permission is decided as usual.
    Returns what was asked, as ``permission@resource``.
    """
    real: Callable[..., Any] = artifact_commands.authorize
    asked: list[str] = []
    scoped = {"artifacts.read", "tasks.read"}

    async def authorize(ctx: Any, *any_of: Any, resource: Any = None, **kwargs: Any) -> None:
        names = {permission.value for permission in any_of}
        allowed = grants.get(str(ctx.principal_id))
        if allowed is not None and names & scoped:
            key = resource.key if resource is not None else f"tenant:{ctx.tenant_id}"
            asked.append(f"{','.join(sorted(names))}@{key}")
            if key not in allowed:
                raise AuthorizationError(details={"required": sorted(names), "resource": key})
            return
        await real(ctx, *any_of, resource=resource, **kwargs)

    monkeypatch.setattr(artifact_commands, "authorize", authorize)
    return asked


async def read_both(client: httpx.AsyncClient, key: str, artifact_id: str) -> tuple[int, int]:
    record = await client.get(f"/api/v1/artifacts/{artifact_id}", headers=auth(key))
    content = await client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=auth(key))
    return record.status_code, content.status_code


async def test_executor_reads_with_artifacts_read_on_the_task_workspace(
    client: httpx.AsyncClient, boot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = pdp_binding(monkeypatch, {boot["executorId"]: {f"workspace:{boot['docs']}"}})
    url = f"/api/v1/artifacts/{boot['artifact']}"

    record = await client.get(url, headers=auth(boot["executor"]))
    assert record.status_code == 200, record.text
    assert record.json()["id"] == boot["artifact"]
    content = await client.get(f"{url}/content", headers=auth(boot["executor"]))
    assert content.status_code == 200, content.text
    assert content.content == DATA

    # Decided on the task first, then on the task's workspace.
    assert asked[:2] == [
        f"artifacts.read@task:{boot['task']}",
        f"artifacts.read@workspace:{boot['docs']}",
    ]
    reads = await events_of(client, boot["admin"], "artifact.content_read")
    assert len(reads) == 1
    assert reads[0]["actorId"] == boot["executorId"]
    assert reads[0]["payload"]["taskId"] == boot["task"]
    assert reads[0]["payload"]["forTaskId"] is None

    # The right is on the artifacts, not on the task.
    task = await client.get(f"/api/v1/tasks/{boot['task']}", headers=auth(boot["executor"]))
    assert task.status_code == 403


async def test_executor_without_the_right_is_denied(
    client: httpx.AsyncClient, boot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pdp_binding(monkeypatch, {boot["executorId"]: set()})
    assert await read_both(client, boot["executor"], boot["artifact"]) == (403, 403)
    denied = await client.get(
        f"/api/v1/artifacts/{boot['artifact']}/content", headers=auth(boot["executor"])
    )
    assert denied.json()["error"]["code"] == "permission_denied"
    # The refusal names the artifact's task, as before the amendment.
    assert denied.json()["error"]["details"]["resource"] == f"task:{boot['task']}"
    assert await events_of(client, boot["admin"], "artifact.content_read") == []


async def test_executor_with_the_right_on_another_workspace_is_denied(
    client: httpx.AsyncClient, boot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pdp_binding(monkeypatch, {boot["executorId"]: {f"workspace:{boot['other']}"}})
    assert await read_both(client, boot["executor"], boot["artifact"]) == (403, 403)


async def test_workspace_right_without_skills_execute_gives_no_path(
    client: httpx.AsyncClient, boot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    person, person_key = await create_agent_with_key(
        client, boot["admin"], name="reader", permissions=["artifacts.read", "tasks.read"]
    )
    asked = pdp_binding(monkeypatch, {person["id"]: {f"workspace:{boot['docs']}"}})
    assert await read_both(client, person_key, boot["artifact"]) == (403, 403)
    # The workspace is never asked: the path starts with skills.execute.
    assert f"artifacts.read@workspace:{boot['docs']}" not in asked


async def test_another_tenant_gets_404(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdp_binding(monkeypatch, {boot["executorId"]: {f"workspace:{boot['docs']}"}})
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    assert await read_both(client, key_b, boot["artifact"]) == (404, 404)


async def test_local_mode_needs_both_flat_rights(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    # Without a PDP the executor's key decides: skills.execute and artifacts.read.
    assert await read_both(client, boot["executor"], boot["artifact"]) == (200, 200)
    _, bare = await create_agent_with_key(
        client, boot["admin"], name="bare-executor", permissions=["skills.execute"]
    )
    assert await read_both(client, bare, boot["artifact"]) == (403, 403)
