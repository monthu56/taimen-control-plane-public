"""Scenario E: the autonomous reference harness performs the same
discover → claim → run → artifact → complete cycle with no special server
path — protocol symmetry between humans and agents."""

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from control_plane_agent.main import Agent, ArtifactSpec, EchoAdapter
from control_plane_agent.supervision import SupervisionSettings
from control_plane_agent.workspace import ExecutionWorkspacePool, Workspace
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

# A runner reads task types to tell skill-executed Work apart (ADR-0056 §3);
# without the right it takes no typed task at all.
RUNNER_PERMISSIONS = [*ORG_AGENT_PERMISSIONS, "task_types.read"]


async def test_agent_full_cycle(client: httpx.AsyncClient, sdk: Make) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Automatable chore")

    async with sdk(agent_key) as sdk:
        agent = Agent(sdk, EchoAdapter(), poll_interval=0.05, max_cycles=3)
        await agent.run_forever()

    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert record["status"] == "done"
    assert record["activeClaimId"] is None

    runs = (
        await client.get("/api/v1/runs", params={"taskId": task["id"]}, headers=auth(admin_key))
    ).json()["items"]
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"

    artifacts = (
        await client.get(
            "/api/v1/artifacts", params={"taskId": task["id"]}, headers=auth(admin_key)
        )
    ).json()["items"]
    assert [a["type"] for a in artifacts] == ["report"]

    actions = (
        await client.get(f"/api/v1/runs/{runs[0]['id']}/actions", headers=auth(admin_key))
    ).json()["items"]
    assert [a["action"] for a in actions] == ["echo.observe"]


async def test_agent_without_task_types_read_leaves_typed_work_alone(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    """Not knowing whether a skill executes the task is not a reason to run it as code."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=ORG_AGENT_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Maybe a skill's")

    async with sdk(agent_key) as sdk:
        agent = Agent(sdk, EchoAdapter(), poll_interval=0.05, max_cycles=2)
        await agent.run_forever()

    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert record["activeClaimId"] is None
    runs = (
        await client.get("/api/v1/runs", params={"taskId": task["id"]}, headers=auth(admin_key))
    ).json()["items"]
    assert runs == []


async def test_agent_respects_workspace_scope(client: httpx.AsyncClient, sdk: Make) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    eng = await create_workspace(client, admin_key, "engineering")
    inside = await create_task(client, admin_key, title="In scope", workspaceId=eng["id"])
    outside = await create_task(client, admin_key, title="Out of scope")

    async with sdk(agent_key) as sdk:
        agent = Agent(sdk, EchoAdapter(), poll_interval=0.05, max_cycles=3, workspace_id=eng["id"])
        await agent.run_forever()

    inside_status = (
        await client.get(f"/api/v1/tasks/{inside['id']}", headers=auth(admin_key))
    ).json()["status"]
    outside_status = (
        await client.get(f"/api/v1/tasks/{outside['id']}", headers=auth(admin_key))
    ).json()["status"]
    assert inside_status == "done"
    assert outside_status == "todo"


async def test_agent_restart_recovery(client: httpx.AsyncClient, sdk: Make, sync_engine) -> None:
    """A crashed agent's successor reaps ORPHANED work (dead session) only."""
    from tests.helpers import backdate_expiry

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Crashed mid-flight")

    async with sdk(agent_key) as sdk:
        # Crashed predecessor: claim + run left behind, its session lease dead.
        session = await sdk.open_session(client_name="bot-crashed")
        claim = await sdk.claim_task(task["id"], session["id"])
        await sdk.start_run(task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"])
        backdate_expiry(sync_engine, "sessions", session["id"])

        # The restarted agent recovers the orphan, then completes the task.
        agent = Agent(sdk, EchoAdapter(), poll_interval=0.05, max_cycles=3)
        await agent.run_forever()

    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert record["status"] == "done"
    runs = (
        await client.get("/api/v1/runs", params={"taskId": task["id"]}, headers=auth(admin_key))
    ).json()["items"]
    statuses = sorted(r["status"] for r in runs)
    assert statuses == ["failed", "succeeded"]  # honest recovery + real completion


async def test_agent_recovery_spares_live_sibling(client: httpx.AsyncClient, sdk: Make) -> None:
    """recover() must NOT kill work held by a live session of the same
    principal (a concurrently running sibling instance)."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Sibling is working on this")

    async with sdk(agent_key) as sdk:
        sibling_session = await sdk.open_session(client_name="bot-sibling")
        sibling_claim = await sdk.claim_task(task["id"], sibling_session["id"])
        await sdk.start_run(
            task["id"],
            claim_id=sibling_claim["id"],
            fencing_token=sibling_claim["fencingToken"],
        )

        agent = Agent(sdk, EchoAdapter(), poll_interval=0.05, max_cycles=1)
        await agent.run_forever()

        # The sibling's run is untouched and still running.
        runs = (
            await client.get("/api/v1/runs", params={"taskId": task["id"]}, headers=auth(admin_key))
        ).json()["items"]
        assert [r["status"] for r in runs] == ["running"]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class FileWritingAdapter:
    """Adapter that actually changes files — inside its own copy, nowhere else."""

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        assert workspace is not None
        (workspace.path / "result.txt").write_text(task["publicId"])
        return []


async def test_agent_commits_its_work_and_registers_it_by_reference(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    """The execution workspace closes the loop: file change → commit → artifact.

    What the Control Plane learns is a reference (``git:<sha>``) and a branch,
    never the content and never a local path (ADR-0016 §5, harness-protocol §8).
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    (origin / "README.md").write_text("origin\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "initial")

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Change a file")
    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces")

    async with sdk(agent_key) as sdk_client:
        agent = Agent(
            sdk_client, FileWritingAdapter(), poll_interval=0.05, max_cycles=1, workspaces=pool
        )
        await agent.run_forever()

    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert record["status"] == "done"
    public_id = record["publicId"]

    artifacts = (
        await client.get(
            "/api/v1/artifacts", params={"taskId": task["id"]}, headers=auth(admin_key)
        )
    ).json()["items"]
    commits = [a for a in artifacts if a["type"] == "commit"]
    assert len(commits) == 1
    branch = f"task/{public_id}"
    sha = _git(origin, "rev-parse", branch)
    assert commits[0]["uri"] == f"git:{sha}"
    assert str(tmp_path) not in str(commits[0])  # no local path leaves the host

    # The commit is on the task branch and carries exactly this task's change.
    assert public_id in _git(origin, "log", "-1", "--format=%B", branch)
    assert _git(origin, "show", "--name-only", "--format=", branch) == "result.txt"

    runs = (
        await client.get("/api/v1/runs", params={"taskId": task["id"]}, headers=auth(admin_key))
    ).json()["items"]
    checkpoints = (
        await client.get(f"/api/v1/runs/{runs[0]['id']}/checkpoints", headers=auth(admin_key))
    ).json()["items"]
    workspace_checkpoints = [c for c in checkpoints if c["kind"] == "execution.workspace"]
    assert workspace_checkpoints, "the workspace must be recoverable from the run"
    assert workspace_checkpoints[-1]["data"]["branch"] == branch
    assert workspace_checkpoints[-1]["data"]["head"] == sha


# --- supervision: cancellation and the no-progress watchdog ---------------------


class StubAdapter:
    """Works until stopped; records ``actions`` actions, ``pace`` seconds apart."""

    def __init__(self, *, actions: int = 0, pace: float = 0.05, finish: bool = False) -> None:
        self.actions = actions
        self.pace = pace
        self.finish = finish
        self.started = asyncio.Event()
        self.stopped = False
        self.run_id = ""

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        self.run_id = str(run["id"])
        self.started.set()
        try:
            for index in range(self.actions):
                await client.record_action(run["id"], action=f"stub.step.{index}")
                await asyncio.sleep(self.pace)
            if not self.finish:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.stopped = True
            raise
        return []


async def _runs(client: httpx.AsyncClient, key: str, task_id: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/runs", params={"taskId": task_id}, headers=auth(key))
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _run_events(
    client: httpx.AsyncClient, key: str, run_id: str, event_type: str
) -> list[dict[str, Any]]:
    items = (await client.get("/api/v1/events?limit=200&entityType=run", headers=auth(key))).json()[
        "items"
    ]
    return [e for e in items if e["type"] == event_type and e["entityId"] == run_id]


async def test_a_cancel_request_stops_the_adapter_and_cancels_the_run_once(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="No longer needed")
    adapter = StubAdapter()
    settings = SupervisionSettings(poll_seconds=0.05, stall_warn_seconds=0, stall_stop_seconds=0)

    async with sdk(agent_key) as agent_sdk:
        agent = Agent(agent_sdk, adapter, poll_interval=0.05, max_cycles=1, supervision=settings)
        running = asyncio.ensure_future(agent.run_forever())
        await asyncio.wait_for(adapter.started.wait(), 10)
        asked = await client.post(
            f"/api/v1/runs/{adapter.run_id}:request-cancel",
            json={"reason": "the premise is gone"},
            headers=auth(admin_key),
        )
        assert asked.status_code == 200, asked.text
        await asyncio.wait_for(running, 10)

    assert adapter.stopped
    [run] = await _runs(client, admin_key, task["id"])
    assert (run["status"], run["failureReason"]) == ("cancelled", "cancel_requested")
    assert len(await _run_events(client, admin_key, run["id"], "run.cancelled")) == 1
    controls = (
        await client.get(f"/api/v1/runs/{run['id']}/control-messages", headers=auth(admin_key))
    ).json()["items"]
    assert [(c["operation"], c["status"]) for c in controls] == [("request_cancel", "applied")]
    # The claim is let go: whatever decided to stop the work can now apply.
    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert record["activeClaimId"] is None
    assert record["status"] == "todo"


async def test_a_run_without_progress_is_warned_about_then_stopped(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Stuck")
    adapter = StubAdapter()
    settings = SupervisionSettings(
        poll_seconds=0.05, stall_warn_seconds=0.2, stall_stop_seconds=0.6
    )

    async with sdk(agent_key) as agent_sdk:
        agent = Agent(agent_sdk, adapter, poll_interval=0.05, max_cycles=1, supervision=settings)
        await asyncio.wait_for(agent.run_forever(), 10)

    assert adapter.stopped
    [run] = await _runs(client, admin_key, task["id"])
    assert (run["status"], run["failureReason"]) == ("failed", "no_progress")
    checkpoints = (
        await client.get(f"/api/v1/runs/{run['id']}/checkpoints", headers=auth(admin_key))
    ).json()["items"]
    stalls = [c for c in checkpoints if c["kind"] == "stall"]
    assert len(stalls) == 1
    assert stalls[0]["data"]["lastAction"] is None
    assert stalls[0]["data"]["idleSeconds"] >= 0
    # Back in the queue for another attempt.
    record = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert (record["activeClaimId"], record["status"]) == (None, "todo")


async def test_a_run_that_keeps_recording_actions_is_not_stopped(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Slow but steady")
    # Works for twice the stop threshold, never quiet for longer than a step.
    adapter = StubAdapter(actions=10, pace=0.1, finish=True)
    settings = SupervisionSettings(
        poll_seconds=0.05, stall_warn_seconds=0.5, stall_stop_seconds=0.5
    )

    async with sdk(agent_key) as agent_sdk:
        agent = Agent(agent_sdk, adapter, poll_interval=0.05, max_cycles=1, supervision=settings)
        await asyncio.wait_for(agent.run_forever(), 10)

    assert not adapter.stopped
    [run] = await _runs(client, admin_key, task["id"])
    assert run["status"] == "succeeded"


class LongActionAdapter:
    """Records one action with ``status`` and works ``hold`` seconds before finishing it."""

    def __init__(self, *, status: str, hold: float) -> None:
        self.status = status
        self.hold = hold
        self.started = asyncio.Event()
        self.stopped = False

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        self.started.set()
        action = await client.record_action(run["id"], action="stub.suite", status=self.status)
        try:
            await asyncio.sleep(self.hold)
        except asyncio.CancelledError:
            self.stopped = True
            raise
        if self.status == "started":
            await client.finish_action(run["id"], action["id"], status="completed")
        return []


async def _stalls(client: httpx.AsyncClient, key: str, run_id: str) -> list[dict[str, Any]]:
    checkpoints = (
        await client.get(f"/api/v1/runs/{run_id}/checkpoints", headers=auth(key))
    ).json()["items"]
    return [c for c in checkpoints if c["kind"] == "stall"]


async def test_an_unfinished_action_is_alive_until_its_own_limit(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="A long test suite")
    # One action runs for three times the stall stop, well within its own limit.
    adapter = LongActionAdapter(status="started", hold=1.2)
    settings = SupervisionSettings(
        poll_seconds=0.05, stall_warn_seconds=0.2, stall_stop_seconds=0.4, action_max_seconds=5
    )

    async with sdk(agent_key) as agent_sdk:
        agent = Agent(agent_sdk, adapter, poll_interval=0.05, max_cycles=1, supervision=settings)
        await asyncio.wait_for(agent.run_forever(), 10)

    assert not adapter.stopped
    [run] = await _runs(client, admin_key, task["id"])
    assert run["status"] == "succeeded"
    # The stall is still reported at the usual threshold, naming the running action.
    [stall] = await _stalls(client, admin_key, run["id"])
    assert stall["data"]["actionRunning"] is True
    assert stall["data"]["lastAction"]["action"] == "stub.suite"
    assert stall["data"]["lastAction"]["status"] == "started"
    started_at = stall["data"]["lastAction"]["startedAt"]
    assert stall["data"]["note"] == f"action still running: stub.suite since {started_at}"


async def test_an_unfinished_action_is_stopped_at_its_own_limit(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="A hung command")
    adapter = LongActionAdapter(status="started", hold=3600)
    settings = SupervisionSettings(
        poll_seconds=0.05, stall_warn_seconds=0.2, stall_stop_seconds=0.3, action_max_seconds=0.8
    )

    async with sdk(agent_key) as agent_sdk:
        agent = Agent(agent_sdk, adapter, poll_interval=0.05, max_cycles=1, supervision=settings)
        began = asyncio.get_running_loop().time()
        await asyncio.wait_for(agent.run_forever(), 10)
        elapsed = asyncio.get_running_loop().time() - began

    assert adapter.stopped
    assert elapsed >= 0.8
    [run] = await _runs(client, admin_key, task["id"])
    assert (run["status"], run["failureReason"]) == ("failed", "no_progress")
    [stall] = await _stalls(client, admin_key, run["id"])
    assert stall["data"]["actionRunning"] is True


async def test_a_finished_action_followed_by_silence_is_stopped_as_before(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Quiet after a step")
    adapter = LongActionAdapter(status="completed", hold=3600)
    # The action limit is generous; it must not apply to a finished action.
    settings = SupervisionSettings(
        poll_seconds=0.05, stall_warn_seconds=0.2, stall_stop_seconds=0.6, action_max_seconds=30
    )

    async with sdk(agent_key) as agent_sdk:
        agent = Agent(agent_sdk, adapter, poll_interval=0.05, max_cycles=1, supervision=settings)
        await asyncio.wait_for(agent.run_forever(), 10)

    assert adapter.stopped
    [run] = await _runs(client, admin_key, task["id"])
    assert (run["status"], run["failureReason"]) == ("failed", "no_progress")
    # On a slow machine the watchdog may already warn while the action is still
    # running; its finish is progress, so a second warning follows. The last one is
    # the silence after the finished action.
    stall = (await _stalls(client, admin_key, run["id"]))[-1]
    assert stall["data"]["lastAction"]["status"] == "completed"
    assert "actionRunning" not in stall["data"]
