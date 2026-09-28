"""The executor says "stopped, not done" (declarative-cycle C006, case TASK-000642).

A run whose executor left the ``blocked`` checkpoint is failed with
``executor_blocked``: the task is not completed, so its acceptance (a review,
a merge) never starts; it goes to the first ``blocked`` status of its
lifecycle with the reason in a comment, the claim is released, and the
daemon does not take it again until a person returns it to work. A run
without the signal behaves as before — even one that changed nothing.
"""

import subprocess
from pathlib import Path
from typing import Any

import httpx

from control_plane_agent.blocked import CHECKPOINT_KIND
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.workspace import ExecutionWorkspacePool, Workspace
from control_plane_client import ControlPlaneClient
from tests.client.test_agent import RUNNER_PERMISSIONS, Make
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap

REASON = "The task asks for two contradictory schemas; a person has to choose one."
REVIEW = {"key": "review", "kind": "human", "description": "A person reviews the branch"}


class ExecutorAdapter:
    """Changes a file and reports; says it is blocked when told to."""

    def __init__(self, *, blocked: bool, change: bool = True) -> None:
        self.blocked = blocked
        self.change = change
        self.runs = 0

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        self.runs += 1
        if workspace is not None and self.change:
            (workspace.path / "partial.txt").write_text("half of it\n")
        if self.blocked:
            # What the agent inside does over MCP: cp_checkpoint(kind="blocked", ...).
            await client.create_checkpoint(
                str(run["id"]), kind=CHECKPOINT_KIND, data={"reason": REASON}
            )
        return [ArtifactSpec(type="report", name="summary", content={"summary": "Stopped."})]


def _origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True, capture_output=True)
    (origin / "README.md").write_text("origin\n")
    for args in (("add", "-A"), ("commit", "-qm", "initial")):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=origin,
            check=True,
            capture_output=True,
        )
    return origin


async def _setup(client: httpx.AsyncClient, **task: Any) -> dict[str, Any]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="bot", permissions=RUNNER_PERMISSIONS
    )
    created = await create_task(client, admin_key, title="Change a file", **task)
    return {"admin": admin_key, "agent": agent_key, "task": created}


async def _get(client: httpx.AsyncClient, key: str, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v1{path}", params=params or None, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def _work(
    sdk: Make, key: str, adapter: ExecutorAdapter, pool: ExecutionWorkspacePool
) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(sdk_client, adapter, poll_interval=0.05, max_cycles=2, workspaces=pool)
        await agent.run_forever()


async def test_a_blocked_run_fails_and_hands_the_task_to_a_person(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client, acceptance=[REVIEW])
    task_id = s["task"]["id"]
    pool = ExecutionWorkspacePool(_origin(tmp_path), tmp_path / "workspaces")
    adapter = ExecutorAdapter(blocked=True)

    # Two cycles: the second finds the task blocked and leaves it alone.
    await _work(sdk, s["agent"], adapter, pool)
    assert adapter.runs == 1

    record = await _get(client, s["admin"], f"/tasks/{task_id}")
    assert (record["status"], record["systemStatusCategory"]) == ("blocked", "blocked")
    assert record["activeClaimId"] is None
    # Not completed, so the acceptance never started: no review was asked for.
    assert record["verification"] is None
    assert (await _get(client, s["admin"], "/approvals", taskId=task_id))["items"] == []

    [run] = (await _get(client, s["admin"], "/runs", taskId=task_id))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", "executor_blocked")
    assert run["output"] == {"reason": REASON}
    comments = (await _get(client, s["admin"], f"/tasks/{task_id}/comments"))["items"]
    assert len(comments) == 1
    assert REASON in comments[0]["body"]
    assert "executor_blocked" in comments[0]["body"]

    # The report goes out; the half-done work is not committed as evidence.
    artifacts = (await _get(client, s["admin"], "/artifacts", taskId=task_id))["items"]
    assert [a["type"] for a in artifacts] == ["report"]

    # A person returns it to work: the next run takes it, on the same copy.
    returned = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "todo"},
        headers={**auth(s["admin"]), "If-Match": f'"task-{record["version"]}"'},
    )
    assert returned.status_code == 200, returned.text
    adapter.blocked = False
    await _work(sdk, s["agent"], adapter, pool)
    assert adapter.runs == 2
    record = await _get(client, s["admin"], f"/tasks/{task_id}")
    # Handed in: now the acceptance starts (the worker then asks the reviewer).
    assert record["verification"]["status"] in ("running", "waiting_human")
    runs = (await _get(client, s["admin"], "/runs", taskId=task_id))["items"]
    assert sorted(r["status"] for r in runs) == ["failed", "succeeded"]


async def test_a_run_without_the_signal_is_the_work_it_did_even_with_no_changes(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    task_id = s["task"]["id"]
    pool = ExecutionWorkspacePool(_origin(tmp_path), tmp_path / "workspaces")

    await _work(sdk, s["agent"], ExecutorAdapter(blocked=False, change=False), pool)

    record = await _get(client, s["admin"], f"/tasks/{task_id}")
    assert record["status"] == "done"
    [run] = (await _get(client, s["admin"], "/runs", taskId=task_id))["items"]
    assert run["status"] == "succeeded"
    artifacts = (await _get(client, s["admin"], "/artifacts", taskId=task_id))["items"]
    assert [a["type"] for a in artifacts] == ["report"]


async def test_a_task_waiting_for_a_person_is_not_taken(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    """``blocked`` is claimable in the core (CP-ADR-0067 §5); the runner leaves it."""
    s = await _setup(client)
    blocked = await client.patch(
        f"/api/v1/tasks/{s['task']['id']}",
        json={"status": "blocked"},
        headers={**auth(s["admin"]), "If-Match": f'"task-{s["task"]["version"]}"'},
    )
    assert blocked.status_code == 200, blocked.text
    adapter = ExecutorAdapter(blocked=False)

    await _work(sdk, s["agent"], adapter, ExecutionWorkspacePool(_origin(tmp_path), tmp_path / "w"))

    assert adapter.runs == 0
    assert (await _get(client, s["admin"], "/runs", taskId=s["task"]["id"]))["items"] == []
