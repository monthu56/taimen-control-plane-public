"""The daemon of an agent works by its revision (CP-ADR-0073 §7-8, declarative-agents D006).

A principal bound to an agent reads its revision with ``GET /agents/me``,
takes the work the revision describes, names the revision on every run, and
ends with exit code 75 after the run in flight when a newer revision is
published — never mixing two revisions in one process. A stop request drains
the run in flight for ``placement.drainSeconds``, then cancels it. A
principal that is no agent keeps the env mode.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from control_plane_agent.main import Agent, ArtifactSpec, EchoAdapter
from control_plane_agent.revision import (
    DEFAULT_DRAIN_SECONDS,
    EXIT_REVISION_CHANGED,
    AgentRevision,
    my_agent,
    settings_of,
)
from control_plane_agent.supervision import SupervisionSettings
from control_plane_agent.workspace import Workspace
from control_plane_client import ControlPlaneClient
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
)

Make = Callable[[str], ControlPlaneClient]

PERMISSIONS = [*ORG_AGENT_PERMISSIONS, "task_types.read"]
FAST = SupervisionSettings(poll_seconds=0.05, stall_warn_seconds=0, stall_stop_seconds=0)


def _spec(workspace_id: str, **overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "displayName": "Echo agent",
        "identity": {"kind": "agent", "permissions": PERMISSIONS},
        "work": {"workspace": workspace_id, "onlyAssigned": True, "taskTypes": ["task"]},
        "executor": {"kind": "echo"},
        "placement": {"replicas": 1, "drainSeconds": 60},
        "state": "running",
    }
    spec.update(overrides)
    return spec


async def _agent(
    client: httpx.AsyncClient, admin_key: str, spec: dict[str, Any]
) -> tuple[str, str]:
    """Publish ``spec`` as agent ``echo``, link its identity; (principal id, its API key)."""
    published = await client.post(
        "/api/v1/agents", json={"key": "echo", "spec": spec}, headers=auth(admin_key)
    )
    assert published.status_code == 201, published.text
    linked = await client.put(
        "/api/v1/agents/echo/identity",
        json={
            "issuer": "https://iam.example.test",
            "iamTenantId": "5f0c9a52-1a8e-4c43-9a55-3d1b8c1e0a01",
            "iamPrincipalId": "5f0c9a52-1a8e-4c43-9a55-3d1b8c1e0a02",
        },
        headers=auth(admin_key),
    )
    assert linked.status_code == 200, linked.text
    principal_id: str = linked.json()["principalId"]
    key = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": PERMISSIONS},
        headers=auth(admin_key),
    )
    assert key.status_code == 201, key.text
    return principal_id, key.json()["key"]


async def _runs(client: httpx.AsyncClient, key: str, task_id: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/runs", params={"taskId": task_id}, headers=auth(key))
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _task(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    record: dict[str, Any] = (
        await client.get(f"/api/v1/tasks/{task_id}", headers=auth(key))
    ).json()
    return record


class ChangingAdapter:
    """Echo work; while the first run is in flight, somebody changes the agent."""

    def __init__(self, change: Callable[[], Any]) -> None:
        self.change = change
        self.runs: list[str] = []

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None = None,
    ) -> list[ArtifactSpec]:
        self.runs.append(str(run["id"]))
        if len(self.runs) == 1:
            await self.change()
        return [ArtifactSpec(type="report", name=f"echo of {task['publicId']}", content={})]


async def test_a_new_revision_during_a_run_ends_the_process_after_it(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "engineering")
    principal_id, agent_key = await _agent(client, admin_key, _spec(workspace["id"]))
    mine = [
        await create_task(
            client,
            admin_key,
            title=f"Mine {i}",
            workspaceId=workspace["id"],
            assigneeId=principal_id,
        )
        for i in range(2)
    ]
    # Not meant for it: onlyAssigned comes from the revision, not the host.
    unassigned = await create_task(client, admin_key, title="Anyone's", workspaceId=workspace["id"])

    async def publish_revision_2() -> None:
        # No placement at all: placed with the defaults, one replica (CP-ADR-0073 p.1).
        changed = _spec(workspace["id"], description="Now with a description")
        del changed["placement"]
        response = await client.post(
            "/api/v1/agents", json={"key": "echo", "spec": changed}, headers=auth(admin_key)
        )
        assert response.status_code == 201, response.text
        assert response.json()["currentRevision"] == 2
        assert (response.json()["state"], response.json()["replicas"]) == ("running", 1)

    adapter = ChangingAdapter(publish_revision_2)
    async with sdk(agent_key) as agent_sdk:
        revision = await my_agent(agent_sdk)
        assert revision is not None
        assert (revision.key, revision.revision) == ("echo", 1)
        agent = Agent(
            agent_sdk,
            adapter,
            poll_interval=0.05,
            max_cycles=10,
            supervision=FAST,
            revision=revision,
            **settings_of(revision).agent_kwargs(),
        )
        await asyncio.wait_for(agent.run_forever(), 20)

    # The run in flight was finished, by the revision it started with; the
    # process then stopped for a restart and took nothing more.
    assert agent.exit_code == EXIT_REVISION_CHANGED
    assert len(adapter.runs) == 1
    runs = [run for task in mine for run in await _runs(client, admin_key, task["id"])]
    [run] = runs
    assert run["status"] == "succeeded"
    assert run["agentRevisionId"] == revision.revision_id
    statuses = sorted([(await _task(client, admin_key, t["id"]))["status"] for t in mine])
    assert statuses == ["done", "todo"]
    assert await _runs(client, admin_key, unassigned["id"]) == []

    # The restarted process reads revision 2 and names it.
    async with sdk(agent_key) as agent_sdk:
        second = await my_agent(agent_sdk)
        assert second is not None and second.revision == 2
        assert "placement" not in second.spec
        assert settings_of(second).drain_seconds == DEFAULT_DRAIN_SECONDS
        agent = Agent(
            agent_sdk,
            EchoAdapter(),
            poll_interval=0.05,
            max_cycles=2,
            supervision=FAST,
            revision=second,
            **settings_of(second).agent_kwargs(),
        )
        await asyncio.wait_for(agent.run_forever(), 20)
    assert agent.exit_code == 0
    runs = [run for task in mine for run in await _runs(client, admin_key, task["id"])]
    assert sorted(run["agentRevisionId"] for run in runs) == sorted(
        [revision.revision_id, second.revision_id]
    )


async def test_a_stopped_agent_finishes_its_run_and_exits_cleanly(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "engineering")
    principal_id, agent_key = await _agent(client, admin_key, _spec(workspace["id"]))
    mine = [
        await create_task(
            client,
            admin_key,
            title=f"Mine {i}",
            workspaceId=workspace["id"],
            assigneeId=principal_id,
        )
        for i in range(2)
    ]

    async def stop() -> None:
        response = await client.patch(
            "/api/v1/agents/echo/state", json={"state": "stopped"}, headers=auth(admin_key)
        )
        assert response.status_code == 200, response.text

    adapter = ChangingAdapter(stop)
    async with sdk(agent_key) as agent_sdk:
        revision = await my_agent(agent_sdk)
        assert revision is not None
        agent = Agent(
            agent_sdk,
            adapter,
            poll_interval=0.05,
            max_cycles=10,
            supervision=FAST,
            revision=revision,
            **settings_of(revision).agent_kwargs(),
        )
        await asyncio.wait_for(agent.run_forever(), 20)

    assert agent.exit_code == 0
    assert len(adapter.runs) == 1
    statuses = sorted([(await _task(client, admin_key, t["id"]))["status"] for t in mine])
    assert statuses == ["done", "todo"]


class EndlessAdapter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = False
        self.run_id = ""

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None = None,
    ) -> list[ArtifactSpec]:
        self.run_id = str(run["id"])
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.stopped = True
            raise
        return []


async def test_a_stop_drains_the_run_then_cancels_it(client: httpx.AsyncClient, sdk: Make) -> None:
    """``placement.drainSeconds``: the run in flight gets that long, then it goes back."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "engineering")
    spec = _spec(workspace["id"], placement={"replicas": 1, "drainSeconds": 0})
    principal_id, agent_key = await _agent(client, admin_key, spec)
    task = await create_task(
        client, admin_key, title="Long", workspaceId=workspace["id"], assigneeId=principal_id
    )

    adapter = EndlessAdapter()
    async with sdk(agent_key) as agent_sdk:
        revision = await my_agent(agent_sdk)
        assert revision is not None
        settings = settings_of(revision)
        assert settings.drain_seconds == 0
        agent = Agent(
            agent_sdk,
            adapter,
            poll_interval=0.05,
            max_cycles=5,
            supervision=FAST,
            revision=revision,
            **settings.agent_kwargs(),
        )
        running = asyncio.ensure_future(agent.run_forever())
        await asyncio.wait_for(adapter.started.wait(), 10)
        agent.request_stop()
        await asyncio.wait_for(running, 10)

    assert adapter.stopped
    [run] = await _runs(client, admin_key, task["id"])
    assert (run["status"], run["failureReason"]) == ("cancelled", "drained")
    assert run["agentRevisionId"] == revision.revision_id
    record = await _task(client, admin_key, task["id"])
    assert (record["activeClaimId"], record["status"]) == (None, "todo")


async def test_a_principal_that_is_no_agent_keeps_the_env_mode(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, runner_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Chore")

    async with sdk(runner_key) as runner_sdk:
        assert await my_agent(runner_sdk) is None
        agent = Agent(runner_sdk, EchoAdapter(), poll_interval=0.05, max_cycles=3)
        await asyncio.wait_for(agent.run_forever(), 20)

    assert agent.exit_code == 0
    [run] = await _runs(client, admin_key, task["id"])
    assert (run["status"], run["agentRevisionId"]) == ("succeeded", None)


def test_revision_body_round_trip() -> None:
    body = {
        "key": "echo",
        "status": "active",
        "state": "running",
        "workspaceId": None,
        "revision": {"id": "r-1", "revision": 3, "specHash": "sha256:x", "spec": {}},
    }
    revision = AgentRevision.from_body(body)
    assert (revision.label, revision.revision_id, revision.retired) == ("echo@3", "r-1", False)


async def test_the_sdk_speaks_the_agents_contract(client: httpx.AsyncClient, sdk: Make) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "engineering")
    spec = _spec(workspace["id"])

    async with sdk(admin_key) as admin:
        checked = await admin.validate_agent("echo", spec)
        assert (checked["wouldCreateRevision"], checked["currentRevision"]) == (True, None)
        first = await admin.publish_agent("echo", spec)
        assert first["currentRevision"] == 1
        assert (await admin.publish_agent("echo", spec))["currentRevision"] == 1
        second = await admin.publish_agent("echo", {**spec, "description": "v2"})
        assert second["revision"]["revision"] == 2
        assert (await admin.get_agent("echo@1"))["revision"]["id"] == first["revision"]["id"]
        assert [a["key"] for a in (await admin.list_agents(status="active"))["items"]] == ["echo"]
        assert (await admin.update_agent_state("echo", replicas=2))["replicas"] == 2
        linked = await admin.link_agent_identity(
            "echo",
            issuer="https://iam.example.test",
            iam_tenant_id="5f0c9a52-1a8e-4c43-9a55-3d1b8c1e0a01",
            iam_principal_id="5f0c9a52-1a8e-4c43-9a55-3d1b8c1e0a02",
        )
        assert linked["principalId"]
        reported = await admin.report_agent_status(
            "echo",
            phase="running",
            instances={"desired": 2, "ready": 1},
            observed_at="2026-09-27T10:00:00Z",
            observed_revision=2,
            node="node-a",
        )
        assert reported["phase"] == "running"
        assert (await admin.get_agent_status("echo"))["observedRevision"] == 2
        retired = await admin.retire_agent("echo", reason="replaced")
        assert retired["status"] == "retired"
