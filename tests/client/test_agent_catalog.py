"""A universal runner: the repository of a task comes from a catalog (universal-runner U005).

One daemon takes tasks of several repositories: the key in the task's
``customFields`` (or an alias of it) picks the entry of the agent's catalog,
and the copy is cut from that entry's repository, never from an address the
task names. A task without a known key goes to a person as
``repository_unknown``; a task whose key changed leaves its old copy behind
only when that copy holds no work, and otherwise goes to a person as
``repository_changed``.
"""

import subprocess
from pathlib import Path
from typing import Any

import httpx

from control_plane_agent.catalog import RepositoryPools
from control_plane_agent.main import Agent, ArtifactSpec
from control_plane_agent.revision import AgentRevision, workspace_pool_of
from control_plane_agent.workspace import Workspace
from control_plane_client import ControlPlaneClient
from tests.client.test_agent import RUNNER_PERMISSIONS, Make
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap


class RecordingAdapter:
    """Writes a file named after the task; commits it itself or stops when told to."""

    def __init__(self, *, blocked: bool = False, change: bool = True, commit: bool = False):
        self.blocked = blocked
        self.change = change
        self.commit = commit
        self.seen: list[tuple[str, str, Path]] = []

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]:
        assert workspace is not None
        self.seen.append((task["publicId"], workspace.repository_key, workspace.path))
        if self.change:
            (workspace.path / f"{task['publicId']}.txt").write_text("work\n")
        if self.commit:
            _git(workspace.path, "add", "-A")
            _git(workspace.path, "commit", "-qm", f"{task['publicId']}: part of it")
        if self.blocked:
            await client.create_checkpoint(
                str(run["id"]), kind="blocked", data={"reason": "a person has to decide"}
            )
        return [ArtifactSpec(type="report", name="summary", content={"summary": "done"})]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _origin(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(f"{path.name}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "initial")
    return path


def _pools(tmp_path: Path) -> tuple[RepositoryPools, dict[str, Path]]:
    origins = {name: _origin(tmp_path / "forge" / name) for name in ("alpha", "beta")}
    revision = AgentRevision(
        key="coder",
        revision=1,
        revision_id="11111111-1111-1111-1111-111111111111",
        spec_hash="sha256:0",
        spec={
            "workingCopy": {
                "repositoryField": "repositoryKey",
                "repositories": {
                    "alpha": {"url": str(origins["alpha"]), "aliases": ["Old-Alpha"]},
                    "beta": {"url": str(origins["beta"]), "baseRef": "main"},
                },
            }
        },
        status="active",
        state="running",
    )
    pools = workspace_pool_of(revision, {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")})
    assert isinstance(pools, RepositoryPools)
    return pools, origins


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, name="coder", permissions=RUNNER_PERMISSIONS
    )
    return {"admin": admin_key, "agent": agent_key}


async def _task(client: httpx.AsyncClient, admin: str, key: Any = None, **extra: Any) -> Any:
    fields = {} if key is None else {"repositoryKey": key}
    return await create_task(client, admin, title="Change a file", customFields=fields, **extra)


async def _get(client: httpx.AsyncClient, key: str, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v1{path}", params=params or None, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


async def _work(
    sdk: Make, key: str, adapter: RecordingAdapter, pools: RepositoryPools, cycles: int = 2
) -> None:
    async with sdk(key) as sdk_client:
        agent = Agent(sdk_client, adapter, poll_interval=0.05, max_cycles=cycles, workspaces=pools)
        await agent.run_forever()


async def _workspace_checkpoints(client: httpx.AsyncClient, admin: str, task_id: str) -> list[Any]:
    found = []
    for run in (await _get(client, admin, "/runs", taskId=task_id))["items"]:
        items = (await _get(client, admin, f"/runs/{run['id']}/checkpoints"))["items"]
        found += [c["data"] for c in items if c["kind"] == "execution.workspace"]
    return found


async def _return_to_work(
    client: httpx.AsyncClient, admin: str, task_id: str, **fields: Any
) -> None:
    record = await _get(client, admin, f"/tasks/{task_id}")
    patched = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "todo", "customFields": fields},
        headers={**auth(admin), "If-Match": f'"task-{record["version"]}"'},
    )
    assert patched.status_code == 200, patched.text


def _branches(origin: Path) -> list[str]:
    return _git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/task/").split()


async def test_tasks_of_two_repositories_in_a_row_in_one_replica(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pools, origins = _pools(tmp_path)
    first = await _task(client, s["admin"], "alpha")
    second = await _task(client, s["admin"], "beta")
    adapter = RecordingAdapter()

    await _work(sdk, s["agent"], adapter, pools, cycles=3)

    assert sorted((key, repo) for key, repo, _ in adapter.seen) == [
        (first["publicId"], "alpha"),
        (second["publicId"], "beta"),
    ]
    # Each task's work is on its branch in its own repository, and only there.
    assert _branches(origins["alpha"]) == [f"task/{first['publicId']}"]
    assert _branches(origins["beta"]) == [f"task/{second['publicId']}"]
    beta_files = _git(origins["beta"], "ls-tree", "--name-only", f"task/{second['publicId']}")
    assert f"{second['publicId']}.txt" in beta_files.split()
    assert f"{first['publicId']}.txt" not in beta_files.split()
    # One container per task under the shared root, the copy in the key's directory.
    paths = {key: path for key, _, path in adapter.seen}
    assert paths[first["publicId"]].name == "alpha"
    assert paths[second["publicId"]].name == "beta"
    assert paths[first["publicId"]].parent.parent == paths[second["publicId"]].parent.parent

    for task, key in ((first, "alpha"), (second, "beta")):
        record = await _get(client, s["admin"], f"/tasks/{task['id']}")
        assert record["status"] == "done"
        [opened, committed] = await _workspace_checkpoints(client, s["admin"], task["id"])
        assert opened["repositoryKey"] == committed["repositoryKey"] == key
        # The revision the branch was cut from: the base of its repository.
        assert opened["baseRevision"] == _git(origins[key], "rev-parse", "main")


async def test_an_alias_names_the_repository_by_its_canonical_key(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pools, origins = _pools(tmp_path)
    task = await _task(client, s["admin"], "old-alpha")  # alias, another case
    adapter = RecordingAdapter()

    await _work(sdk, s["agent"], adapter, pools)

    assert [(key, repo) for key, repo, _ in adapter.seen] == [(task["publicId"], "alpha")]
    assert _branches(origins["alpha"]) == [f"task/{task['publicId']}"]
    checkpoints = await _workspace_checkpoints(client, s["admin"], task["id"])
    assert {c["repositoryKey"] for c in checkpoints} == {"alpha"}


async def test_a_task_without_a_known_key_goes_to_a_person(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pools, origins = _pools(tmp_path)
    unknown = await _task(client, s["admin"], "gamma")
    missing = await _task(client, s["admin"])
    wrong_type = await _task(client, s["admin"], ["alpha"])
    # A URL is not a key: the address of a clone never comes from the task.
    url = await _task(client, s["admin"], str(origins["alpha"]))
    adapter = RecordingAdapter()

    await _work(sdk, s["agent"], adapter, pools, cycles=5)

    assert adapter.seen == []
    for task in (unknown, missing, wrong_type, url):
        record = await _get(client, s["admin"], f"/tasks/{task['id']}")
        assert (record["status"], record["systemStatusCategory"]) == ("blocked", "blocked")
        assert record["activeClaimId"] is None
        [run] = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
        assert (run["status"], run["failureReason"]) == ("failed", "repository_unknown")
        assert "repositoryKey" in run["output"]["reason"]
        [comment] = (await _get(client, s["admin"], f"/tasks/{task['id']}/comments"))["items"]
        assert "repository_unknown" in comment["body"]
        # No local path of this host in durable state.
        assert str(tmp_path) not in comment["body"] + run["output"]["reason"]
    # Nothing was cut anywhere.
    assert _branches(origins["alpha"]) == _branches(origins["beta"]) == []


async def test_a_changed_key_drops_a_copy_without_work_of_its_own(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pools, origins = _pools(tmp_path)
    task = await _task(client, s["admin"], "alpha")
    # The first attempt stops before changing anything: the copy stays, clean.
    adapter = RecordingAdapter(blocked=True, change=False)
    await _work(sdk, s["agent"], adapter, pools)
    [(_, _, old_copy)] = adapter.seen
    assert old_copy.is_dir()
    assert _branches(origins["alpha"]) == [f"task/{task['publicId']}"]

    # A person corrects the key and returns the task.
    await _return_to_work(client, s["admin"], task["id"], repositoryKey="beta")
    adapter.blocked, adapter.change = False, True
    await _work(sdk, s["agent"], adapter, pools)

    assert [repo for _, repo, _ in adapter.seen] == ["alpha", "beta"]
    assert not old_copy.exists()
    assert _branches(origins["alpha"]) == []
    assert _branches(origins["beta"]) == [f"task/{task['publicId']}"]
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert record["status"] == "done"


async def test_a_changed_key_with_commits_of_its_own_goes_to_a_person(
    client: httpx.AsyncClient, sdk: Make, tmp_path: Path
) -> None:
    s = await _setup(client)
    pools, origins = _pools(tmp_path)
    task = await _task(client, s["admin"], "alpha")
    adapter = RecordingAdapter(blocked=True, commit=True)
    await _work(sdk, s["agent"], adapter, pools)
    [(_, _, old_copy)] = adapter.seen
    work = _git(origins["alpha"], "rev-parse", f"task/{task['publicId']}")

    await _return_to_work(client, s["admin"], task["id"], repositoryKey="beta")
    adapter.blocked = False
    await _work(sdk, s["agent"], adapter, pools)

    # The executor never ran in beta, and nothing of alpha was touched.
    assert len(adapter.seen) == 1
    assert old_copy.is_dir()
    assert _git(origins["alpha"], "rev-parse", f"task/{task['publicId']}") == work
    assert _branches(origins["beta"]) == []
    record = await _get(client, s["admin"], f"/tasks/{task['id']}")
    assert (record["status"], record["systemStatusCategory"]) == ("blocked", "blocked")
    runs = (await _get(client, s["admin"], "/runs", taskId=task["id"]))["items"]
    newest = max(runs, key=lambda r: r["attempt"])
    assert (newest["status"], newest["failureReason"]) == ("failed", "repository_changed")
    assert "alpha" in newest["output"]["reason"] and "beta" in newest["output"]["reason"]
    comments = (await _get(client, s["admin"], f"/tasks/{task['id']}/comments"))["items"]
    assert any("repository_changed" in c["body"] for c in comments)

    # Once a person dropped the work, the task goes on in its new repository.
    _git(origins["alpha"], "worktree", "remove", "--force", str(old_copy))
    _git(origins["alpha"], "branch", "-D", f"task/{task['publicId']}")
    await _return_to_work(client, s["admin"], task["id"], repositoryKey="beta")
    await _work(sdk, s["agent"], adapter, pools)
    assert [repo for _, repo, _ in adapter.seen] == ["alpha", "beta"]
    assert (await _get(client, s["admin"], f"/tasks/{task['id']}"))["status"] == "done"
