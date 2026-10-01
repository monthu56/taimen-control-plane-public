"""Replicas of one agent share a queue and a forge (universal-runner U007).

The acceptance of U007, end to end against the core: a refused publish
target pushes nothing and hands the task to a person; a task stopped
``blocked`` on one replica is continued by another — a second pool with its
own mirror — from the WIP the first one published; a run closed by restart
recovery saves its work the same way; a commit the forge did not take goes
out in the next cycle; a replica that listed a task another replica
already took claims the next one instead, as does one the core forbids that
task only; one the core forbids to claim at all stops the cycle.
"""

import logging
import random
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane_agent.blocked import CHECKPOINT_KIND as BLOCKED_KIND
from control_plane_agent.catalog import RepositoryPools
from control_plane_agent.main import WIP_TRAILER, Agent, ArtifactSpec, EchoAdapter
from control_plane_agent.publish import PUBLISH_TARGET_REJECTED, PublishTarget
from control_plane_agent.revision import AgentRevision, workspace_pool_of
from control_plane_agent.workspace import ExecutionWorkspacePool, Workspace
from control_plane_client import (
    ControlPlaneClient,
    NotEligibleError,
    PermissionDeniedError,
)
from tests.client.test_agent import RUNNER_PERMISSIONS, Make
from tests.helpers import auth, backdate_expiry, create_agent_with_key, create_task, do_bootstrap

REASON = "Two schemas contradict each other; a person has to choose."


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _forge(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "initial")
    forge = tmp_path / "forge.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(forge))
    return forge


def _replica(tmp_path: Path, forge: Path, name: str, **kw: Any) -> ExecutionWorkspacePool:
    """A replica's own volume: its mirror of the forge and its working copies."""
    mirror = tmp_path / name / "mirror.git"
    mirror.parent.mkdir()
    _git(tmp_path, "clone", "-q", "--bare", str(forge), str(mirror))
    return ExecutionWorkspacePool(
        mirror,
        tmp_path / name / "workspaces",
        base_ref="main",
        push_remote="origin",
        publish_url=str(forge),
        **kw,
    )


def _in_forge(forge: Path, branch: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=forge,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


class Executor:
    """Writes a file per run; says it is blocked when told to; records what it found."""

    def __init__(self, name: str = "work", *, blocked: bool = False) -> None:
        self.name = name
        self.blocked = blocked
        self.found: list[set[str]] = []

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        assert workspace is not None
        self.found.append({p.name for p in workspace.path.iterdir() if p.suffix == ".txt"})
        (workspace.path / f"{self.name}-{len(self.found)}.txt").write_text("work\n")
        if self.blocked:
            await client.create_checkpoint(
                str(run["id"]), kind=BLOCKED_KIND, data={"reason": REASON}
            )
        return [ArtifactSpec(type="report", name="summary", content={"summary": "report"})]


class Hook:
    def __init__(self, refusal: str | None) -> None:
        self.refusal = refusal
        self.targets: list[PublishTarget] = []

    def check(self, target: PublishTarget) -> str | None:
        self.targets.append(target)
        return self.refusal


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent = await create_agent_with_key(
        client, admin, name="coder", permissions=RUNNER_PERMISSIONS
    )
    return {"admin": admin, "agent": agent}


async def _get(client: httpx.AsyncClient, key: str, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v1{path}", params=params or None, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def _checkpoints(client: httpx.AsyncClient, admin: str, run_id: str) -> list[Any]:
    items = (await _get(client, admin, f"/runs/{run_id}/checkpoints"))["items"]
    return [c["data"] for c in items if c["kind"] == "execution.workspace"]


async def _work(sdk: Make, key: str, adapter: Any, pool: ExecutionWorkspacePool, **kw: Any) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(
            sdk_client,
            adapter,
            poll_interval=0.05,
            max_cycles=kw.pop("cycles", 2),
            workspaces=pool,
            **kw,
        )
        await agent.run_forever()


async def test_a_refused_target_publishes_nothing_and_hands_the_task_to_a_person(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    task = await create_task(client, s["admin"], title="Change a file")
    forge = _forge(tmp_path)
    hook = Hook("org/repo is public")

    await _work(sdk, s["agent"], Executor(), _replica(tmp_path, forge, "one"), publish_hook=hook)

    branch = f"task/{task['publicId']}"
    assert [t.branch for t in hook.targets] == [branch]
    assert hook.targets[0].url == str(forge)
    assert _in_forge(forge, branch) is None
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["systemStatusCategory"] == "blocked"
    [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (run["status"], run["failureReason"]) == ("failed", PUBLISH_TARGET_REJECTED)
    assert "org/repo is public" in run["output"]["reason"]
    artifacts = (await _get(client, s["admin"], "/artifacts", taskId=task["id"]))["items"]
    assert [a["type"] for a in artifacts] == ["report"]


async def test_a_blocked_task_is_continued_by_another_replica_from_the_same_wip(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    task = await create_task(client, s["admin"], title="Change a file")
    forge = _forge(tmp_path)
    branch = f"task/{task['publicId']}"

    first = Executor("first", blocked=True)
    await _work(sdk, s["agent"], first, _replica(tmp_path, forge, "first"))

    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["systemStatusCategory"] == "blocked"
    [blocked_run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert blocked_run["failureReason"] == "executor_blocked"
    wip = (await _checkpoints(client, s["admin"], blocked_run["id"]))[-1]
    assert (wip["wip"], wip["published"]) == (True, True)
    assert _in_forge(forge, branch) == wip["head"]
    assert WIP_TRAILER in _git(forge, "log", "-1", "--format=%B", wip["head"])
    # WIP is not a result: no commit artifact, so no review and no merge take it.
    artifacts = (await _get(client, s["admin"], "/artifacts", taskId=task["id"]))["items"]
    assert [a["type"] for a in artifacts] == ["report"]

    returned = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "todo"},
        headers={**auth(s["admin"]), "If-Match": f'"task-{record["version"]}"'},
    )
    assert returned.status_code == 200, returned.text

    second = Executor("second")
    await _work(sdk, s["agent"], second, _replica(tmp_path, forge, "second"))

    assert second.found == [{"first-1.txt"}]  # the first replica's work, from the forge
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["status"] == "done"
    commits = [
        a
        for a in (await _get(client, s["admin"], "/artifacts", taskId=task["id"]))["items"]
        if a["type"] == "commit"
    ]
    assert len(commits) == 1
    head = commits[0]["metadata"]["commit"]
    assert commits[0]["metadata"]["published"] is True
    assert _git(forge, "rev-parse", f"{head}^") == wip["head"]
    assert _in_forge(forge, branch) == head


async def test_restart_recovery_saves_the_orphaned_work_before_closing_the_run(
    client: httpx.AsyncClient, sdk: Make, sync_engine: Any, tmp_path: Path
) -> None:
    s = await _setup(client)
    task = await create_task(client, s["admin"], title="Crashed mid-flight")
    forge = _forge(tmp_path)
    pool = _replica(tmp_path, forge, "one")

    async with sdk(s["agent"]) as sdk_client:
        session = await sdk_client.open_session(client_name="coder-crashed")
        claim = await sdk_client.claim_task(task["id"], session["id"])
        run = await sdk_client.start_run(
            task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
        )
        # The crashed process had a copy with work nobody committed.
        workspace = pool.acquire(task["publicId"])
        (workspace.path / "unsaved.txt").write_text("an hour of work\n")
        pool.release(workspace, "failed")
        backdate_expiry(sync_engine, "sessions", session["id"])

        agent = Agent(sdk_client, EchoAdapter(), poll_interval=0.05, max_cycles=0, workspaces=pool)
        await agent.run_forever()

    [closed] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (closed["id"], closed["status"], closed["failureReason"]) == (
        run["id"],
        "failed",
        "restart_recovery",
    )
    head = _in_forge(forge, f"task/{task['publicId']}")
    assert head is not None
    assert _git(forge, "show", f"{head}:unsaved.txt") == "an hour of work"
    # Its claim died with its session: the record goes with the failure.
    wip = closed["output"]["workspace"]
    assert (wip["wip"], wip["head"], wip["published"]) == (True, head, True)
    assert wip["branch"] == f"task/{task['publicId']}"


async def test_an_unpublished_commit_goes_out_in_the_next_cycle(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    task = await create_task(client, s["admin"], title="Change a file")
    forge = _forge(tmp_path)
    pool = _replica(tmp_path, forge, "one")
    away = forge.with_name("away.git")
    forge.rename(away)  # the forge is down while the task is handed in

    await _work(sdk, s["agent"], Executor(), pool, cycles=1)

    [commit] = [
        a
        for a in (await _get(client, s["admin"], "/artifacts", taskId=task["id"]))["items"]
        if a["type"] == "commit"
    ]
    assert commit["metadata"]["published"] is False
    away.rename(forge)

    # The run succeeded, yet its copy stays while the branch waits: the list
    # only pushes tasks that have a copy on this replica.
    assert pool.has_copy(task["publicId"])

    # Nothing to take; the cycle starts with what is still to publish.
    await _work(sdk, s["agent"], Executor(), pool, cycles=1)

    assert _in_forge(forge, f"task/{task['publicId']}") == commit["metadata"]["commit"]


async def test_a_copy_waiting_to_be_published_is_not_pruned_by_the_budget(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    """More tasks handed in than the pool keeps copies of (review of U007): the
    copy of a branch still to publish stays, and the branch goes out."""
    s = await _setup(client)
    one = await create_task(client, s["admin"], title="One")
    two = await create_task(client, s["admin"], title="Two")
    forge = _forge(tmp_path)
    pool = _replica(tmp_path, forge, "one", max_workspaces=0)
    away = forge.with_name("away.git")
    forge.rename(away)  # the forge is down while both are handed in

    await _work(sdk, s["agent"], Executor(), pool, cycles=2)

    # The second release ran the budget over the first idle copy.
    assert pool.has_copy(one["publicId"]) and pool.has_copy(two["publicId"])
    away.rename(forge)
    # The first entry failed again at the start of the second cycle: past its pause.
    later = time.time() + 3600
    await _work(sdk, s["agent"], Executor(), pool, cycles=1, clock=lambda: later)

    for task in (one, two):
        branch = f"task/{task['publicId']}"
        assert _in_forge(forge, branch) == pool.branch_head(branch)
        comments = (await _get(client, s["admin"], f"/tasks/{task['id']}/comments"))["items"]
        assert not any("not published" in c["body"] for c in comments)


class _StaleListing:
    """A replica's client that listed work before another replica took some of it."""

    def __init__(self, inner: ControlPlaneClient, page: dict[str, Any]) -> None:
        self._inner = inner
        self._page = page
        self.claims: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def list_available_work(self, **_: Any) -> dict[str, Any]:
        return self._page

    async def claim_task(self, task_id: str, *args: Any, **kwargs: Any) -> Any:
        self.claims.append(task_id)
        return await self._inner.claim_task(task_id, *args, **kwargs)


class _Forbidden(_StaleListing):
    """A replica whose claims the core refuses outright."""

    async def claim_task(self, task_id: str, *args: Any, **kwargs: Any) -> Any:
        self.claims.append(task_id)
        raise PermissionDeniedError("forbidden", "tasks.claim is not granted", status=403)


class _ForbiddenFor(_StaleListing):
    """A replica the core refuses some claims: ``refusals`` by task id."""

    def __init__(
        self, inner: ControlPlaneClient, page: dict[str, Any], refusals: dict[str, Exception]
    ) -> None:
        super().__init__(inner, page)
        self.refusals = refusals

    async def claim_task(self, task_id: str, *args: Any, **kwargs: Any) -> Any:
        if task_id in self.refusals:
            self.claims.append(task_id)
            raise self.refusals[task_id]
        return await super().claim_task(task_id, *args, **kwargs)


class _Tries(random.Random):
    """Puts one task first, as a shuffle may."""

    def __init__(self, first: str) -> None:
        super().__init__(0)
        self.first = first

    def shuffle(self, x: list[Any]) -> None:  # type: ignore[override]
        x.sort(key=lambda item: item["id"] != self.first)


async def test_two_replicas_do_not_claim_one_task(client: httpx.AsyncClient, sdk: Make) -> None:
    s = await _setup(client)
    one = await create_task(client, s["admin"], title="One")
    two = await create_task(client, s["admin"], title="Two")

    async with sdk(s["agent"]) as first_client, sdk(s["agent"]) as second_client:
        page = await second_client.list_available_work(limit=50)
        assert {t["id"] for t in page["items"]} == {one["id"], two["id"]}
        first = Agent(first_client, EchoAdapter(), rng=_Tries(one["id"]))
        assert await first.run_once()

        stale = _StaleListing(second_client, page)
        second = Agent(stale, EchoAdapter(), rng=_Tries(one["id"]))  # type: ignore[arg-type]
        assert await second.run_once()
        for agent in (first, second):
            assert agent._session_heartbeats is not None
            await agent._session_heartbeats.stop()

    # The second replica reached for the same task, lost it, and took the next.
    assert stale.claims == [one["id"], two["id"]]
    for task in (one, two):
        runs = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
        assert [r["status"] for r in runs] == ["succeeded"]


async def test_a_forbidden_claim_stops_the_cycle(
    client: httpx.AsyncClient,
    sdk: Make,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 is not a lost race (review of attempt #2): every next claim would be refused."""
    # Alembic's fileConfig in an earlier migration test disables existing loggers.
    monkeypatch.setattr(logging.getLogger("control_plane_agent"), "disabled", False)
    s = await _setup(client)
    one = await create_task(client, s["admin"], title="One")
    await create_task(client, s["admin"], title="Two")
    await create_task(client, s["admin"], title="Three")

    async with sdk(s["agent"]) as sdk_client:
        page = await sdk_client.list_available_work(limit=50)
        forbidden = _Forbidden(sdk_client, page)
        agent = Agent(forbidden, EchoAdapter(), rng=_Tries(one["id"]))  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger="control_plane_agent"):
            assert await agent.run_once() is False
        assert agent._session_heartbeats is not None
        await agent._session_heartbeats.stop()

    assert forbidden.claims == [one["id"]]
    assert "no more claims this cycle" in caplog.text


def _on_task(task_id: str) -> Exception:
    return PermissionDeniedError(
        "permission_denied",
        "Insufficient permissions",
        status=403,
        details={"required": ["tasks.claim"], "resource": f"task:{task_id}"},
    )


def _not_eligible(task_id: str) -> Exception:
    return NotEligibleError("not_eligible", "requirements not met", status=403)


@pytest.mark.parametrize("refusal", [_on_task, _not_eligible], ids=["scoped", "not_eligible"])
async def test_a_claim_forbidden_for_one_task_goes_to_the_next(
    client: httpx.AsyncClient, sdk: Make, refusal: Any
) -> None:
    """A scoped binding or the task's requirements refuse that task only (review of U007)."""
    s = await _setup(client)
    one = await create_task(client, s["admin"], title="One")
    two = await create_task(client, s["admin"], title="Two")

    async with sdk(s["agent"]) as sdk_client:
        page = await sdk_client.list_available_work(limit=50)
        forbidden = _ForbiddenFor(sdk_client, page, {one["id"]: refusal(one["id"])})
        agent = Agent(forbidden, EchoAdapter(), rng=_Tries(one["id"]))  # type: ignore[arg-type]
        assert await agent.run_once()
        assert agent._session_heartbeats is not None
        await agent._session_heartbeats.stop()

    assert forbidden.claims == [one["id"], two["id"]]
    assert (await _get(client, s["admin"], "/runs", taskId=one["id"]))["items"] == []
    runs = (await _get(client, s["admin"], "/runs", taskId=two["id"]))["items"]
    assert [r["status"] for r in runs] == ["succeeded"]


async def test_a_claim_forbidden_at_tenant_level_after_a_task_refusal_stops_the_cycle(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    """A task refusal moves on; the next one, about the tenant, still stops the cycle."""
    s = await _setup(client)
    one = await create_task(client, s["admin"], title="One")
    two = await create_task(client, s["admin"], title="Two")
    three = await create_task(client, s["admin"], title="Three")
    tenant = PermissionDeniedError(
        "permission_denied",
        "Insufficient permissions",
        status=403,
        details={"required": ["tasks.claim"], "resource": "tenant:t"},
    )

    async with sdk(s["agent"]) as sdk_client:
        page = await sdk_client.list_available_work(limit=50)
        refusals = {one["id"]: _on_task(one["id"]), two["id"]: tenant, three["id"]: tenant}
        forbidden = _ForbiddenFor(sdk_client, page, refusals)
        agent = Agent(forbidden, EchoAdapter(), rng=_Tries(one["id"]))  # type: ignore[arg-type]
        assert await agent.run_once() is False
        assert agent._session_heartbeats is not None
        await agent._session_heartbeats.stop()

    assert len(forbidden.claims) == 2 and forbidden.claims[0] == one["id"]


async def test_restart_recovery_finds_the_copy_of_a_catalog_task_by_its_key(
    client: httpx.AsyncClient, sdk: Make, sync_engine: Any, tmp_path: Path
) -> None:
    s = await _setup(client)
    task = await create_task(
        client, s["admin"], title="Crashed mid-flight", customFields={"repositoryKey": "alpha"}
    )
    forge = _forge(tmp_path)
    revision = AgentRevision(
        key="coder",
        revision=1,
        revision_id="11111111-1111-1111-1111-111111111111",
        spec_hash="sha256:0",
        spec={
            "workingCopy": {
                "repositoryField": "repositoryKey",
                "repositories": {"alpha": {"url": str(forge), "baseRef": "main"}},
            }
        },
        status="active",
        state="running",
    )
    pools = workspace_pool_of(revision, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")})
    assert isinstance(pools, RepositoryPools)
    pool = pools.pool_for(pools.catalog.entries["alpha"])

    async with sdk(s["agent"]) as sdk_client:
        session = await sdk_client.open_session(client_name="coder-crashed")
        claim = await sdk_client.claim_task(task["id"], session["id"])
        run = await sdk_client.start_run(
            task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
        )
        workspace = pool.acquire(task["publicId"])
        await sdk_client.create_checkpoint(
            run["id"], kind="execution.workspace", data=workspace.checkpoint_data
        )
        (workspace.path / "unsaved.txt").write_text("an hour of work\n")
        pool.release(workspace, "failed")
        backdate_expiry(sync_engine, "sessions", session["id"])

        agent = Agent(sdk_client, EchoAdapter(), max_cycles=0, workspaces=pools)
        await agent.run_forever()

    [closed] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert closed["failureReason"] == "restart_recovery"
    wip = closed["output"]["workspace"]
    assert (wip["wip"], wip["repositoryKey"]) == (True, "alpha")
    branch = f"task/{task['publicId']}"
    assert _git(pool.origin, "show", f"{branch}:unsaved.txt") == "an hour of work"


async def _crashed_run(
    client: httpx.AsyncClient, sdk: Make, sync_engine: Any, s: dict[str, Any], pool: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A run whose replica died holding a copy (on ``pool``) with unsaved work."""
    task = await create_task(client, s["admin"], title="Crashed mid-flight")
    async with sdk(s["agent"]) as sdk_client:
        session = await sdk_client.open_session(client_name="coder-crashed")
        claim = await sdk_client.claim_task(task["id"], session["id"])
        run = await sdk_client.start_run(
            task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
        )
    workspace = pool.acquire(task["publicId"])
    (workspace.path / "unsaved.txt").write_text("an hour of work\n")
    pool.release(workspace, "failed")
    backdate_expiry(sync_engine, "sessions", session["id"])
    return task, run


async def test_an_orphaned_run_is_left_to_the_replica_that_has_its_copy(
    client: httpx.AsyncClient, sdk: Make, sync_engine: Any, tmp_path: Path
) -> None:
    """Replicas restarted together: the one without the copy does not close the run first."""
    s = await _setup(client)
    forge = _forge(tmp_path)
    one = _replica(tmp_path, forge, "one")
    task, run = await _crashed_run(client, sdk, sync_engine, s, one)

    await _work(sdk, s["agent"], EchoAdapter(), _replica(tmp_path, forge, "two"), cycles=1)

    [waiting] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (waiting["id"], waiting["status"]) == (run["id"], "running")

    await _work(sdk, s["agent"], EchoAdapter(), one, cycles=0)

    [closed] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    assert (closed["status"], closed["failureReason"]) == ("failed", "restart_recovery")
    assert closed["output"]["workspace"]["wip"] is True
    head = _in_forge(forge, f"task/{task['publicId']}")
    assert head is not None
    assert _git(forge, "show", f"{head}:unsaved.txt") == "an hour of work"


async def test_an_orphaned_run_whose_replica_is_gone_is_closed_after_the_pause(
    client: httpx.AsyncClient, sdk: Make, sync_engine: Any, tmp_path: Path
) -> None:
    s = await _setup(client)
    forge = _forge(tmp_path)
    task, run = await _crashed_run(client, sdk, sync_engine, s, _replica(tmp_path, forge, "one"))

    await _work(sdk, s["agent"], EchoAdapter(), _replica(tmp_path, forge, "two"), orphan_grace=0.01)

    runs = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    closed = next(r for r in runs if r["id"] == run["id"])
    assert (closed["status"], closed["failureReason"]) == ("failed", "restart_recovery")
    assert not (closed.get("output") or {}).get("workspace")
