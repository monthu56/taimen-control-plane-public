"""The daemon's working copy as evidence: the commit, where it lives, where it goes.

The commit artifact carries what a merge needs — the branch, a shareable
repository URL and the target branch — because the acceptance of the task
type hands exactly these to its merge skill (CP-ADR-0067, amendment
2026-09-27). A task taken again after its verification failed continues its
published branch ``task/<publicId>`` instead of forking a second line.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from control_plane_agent.main import Agent
from control_plane_agent.workspace import ExecutionWorkspacePool, WorkspaceError

TASK = {
    "id": "11111111-1111-1111-1111-111111111111",
    "publicId": "TASK-000100",
    "title": "Runner adapter",
    "typeKey": "coding-task",
}


class _CheckpointClient:
    async def create_checkpoint(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        return {}


def _run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def origin_with_forge(tmp_path: Path) -> Path:
    """A repository whose push remote is a forge URL with a token in it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    forge = tmp_path / "forge.git"
    for args in (
        ("init", "-q", str(origin)),
        ("init", "-q", "--bare", str(forge)),
    ):
        subprocess.run(["git", *args], check=True, capture_output=True)
    (origin / "README.md").write_text("origin\n")
    for args in (
        ("add", "-A"),
        ("commit", "-qm", "initial"),
        ("remote", "add", "forge", "https://bot:token@forge.invalid/org/repo.git"),
        # Pushes land in the local bare repository; the URL others see is the forge's.
        ("config", "remote.forge.pushurl", str(forge)),
    ):
        _run_git(origin, *args)
    return origin


def _agent(pool: ExecutionWorkspacePool) -> Agent:
    agent = object.__new__(Agent)
    agent.client = _CheckpointClient()  # type: ignore[assignment]
    agent.workspaces = pool
    return agent


async def test_commit_evidence_says_where_the_branch_lives_and_where_it_goes(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    """Repository (as a shareable URL) and target branch travel with the commit."""
    pool = ExecutionWorkspacePool(origin_with_forge, tmp_path / "workspaces", push_remote="forge")
    workspace = pool.acquire("TASK-000100")
    (workspace.path / "feature.txt").write_text("work\n")

    (spec,) = await _agent(pool)._commit_evidence(
        {"publicId": "TASK-000100", "title": "Work"}, {"id": "run-1"}, workspace
    )

    assert spec.metadata["published"] is True
    assert spec.metadata["repository"] == "https://forge.invalid/org/repo.git"
    assert spec.metadata["targetBranch"] == pool.base_branch
    assert spec.metadata["branch"] == workspace.branch
    assert "token" not in str(spec.metadata)


async def test_commit_evidence_does_not_share_a_remote_nobody_else_can_reach(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    """A push remote that is a local path: published, but no repository to merge from."""
    _run_git(origin_with_forge, "remote", "add", "local", str(tmp_path / "forge.git"))
    pool = ExecutionWorkspacePool(origin_with_forge, tmp_path / "workspaces", push_remote="local")
    workspace = pool.acquire("TASK-000101")
    (workspace.path / "feature.txt").write_text("work\n")

    (spec,) = await _agent(pool)._commit_evidence(
        {"publicId": "TASK-000101", "title": "Work"}, {"id": "run-1"}, workspace
    )

    assert spec.metadata["published"] is True
    # The merge then fails visibly (unresolved_expression) instead of merging
    # from a path on this host.
    assert "repository" not in spec.metadata
    assert str(tmp_path) not in str(spec.metadata)
    assert spec.metadata["targetBranch"] == pool.base_branch


async def test_commit_evidence_without_a_push_remote_says_nothing_about_merging(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    pool = ExecutionWorkspacePool(origin_with_forge, tmp_path / "workspaces")
    workspace = pool.acquire("TASK-000102")
    (workspace.path / "feature.txt").write_text("work\n")

    (spec,) = await _agent(pool)._commit_evidence(
        {"publicId": "TASK-000102", "title": "Work"}, {"id": "run-1"}, workspace
    )

    assert spec.metadata["published"] is False
    assert "repository" not in spec.metadata
    assert "targetBranch" not in spec.metadata


async def test_work_on_a_feature_branch_goes_back_into_it(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    """customFields.baseBranch: the copy is cut from it and the merge targets it."""
    # The forge URL is unreachable here, so the feature branch the mirror
    # already holds is what the copy is cut from (fetch fallback).
    _run_git(origin_with_forge, "branch", "feature/x")
    task = {**TASK, "customFields": {"baseBranch": "feature/x"}}
    agent = _agent(
        ExecutionWorkspacePool(origin_with_forge, tmp_path / "workspaces", push_remote="forge")
    )

    workspace = await agent._open_workspace(task, {"id": "run-1"})
    assert workspace is not None
    assert workspace.base_branch == "feature/x"
    (workspace.path / "feature.txt").write_text("work\n")
    (spec,) = await agent._commit_evidence(task, {"id": "run-1"}, workspace)

    assert spec.metadata["targetBranch"] == "feature/x"


async def test_a_task_on_a_missing_base_branch_does_not_get_a_copy(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    agent = _agent(ExecutionWorkspacePool(origin_with_forge, tmp_path / "workspaces"))
    task = {**TASK, "customFields": {"baseBranch": "feature/nope"}}

    with pytest.raises(WorkspaceError, match="base branch feature/nope does not exist"):
        await agent._open_workspace(task, {"id": "run-1"})


# --- taking the task again ---------------------------------------------------------


@pytest.fixture
def forge(tmp_path: Path) -> Path:
    """A bare forge holding ``main``, reachable by path from two runners."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _run_git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("seed\n")
    _run_git(seed, "add", "-A")
    _run_git(seed, "commit", "-qm", "initial")
    forge = tmp_path / "forge.git"
    _run_git(tmp_path, "clone", "-q", "--bare", str(seed), str(forge))
    return forge


def _runner(tmp_path: Path, forge: Path, name: str) -> ExecutionWorkspacePool:
    mirror = tmp_path / f"{name}.git"
    _run_git(tmp_path, "clone", "-q", "--bare", str(forge), str(mirror))
    return ExecutionWorkspacePool(
        mirror, tmp_path / f"{name}-workspaces", base_ref="main", push_remote="origin"
    )


async def test_a_task_taken_again_elsewhere_continues_its_published_branch(
    forge: Path, tmp_path: Path
) -> None:
    """Returned by its verification and taken by a runner that never had the branch."""
    first = _runner(tmp_path, forge, "first")
    workspace = first.acquire("TASK-000103")
    (workspace.path / "feature.txt").write_text("first attempt\n")
    (spec,) = await _agent(first)._commit_evidence(
        {"publicId": "TASK-000103", "title": "Work"}, {"id": "run-1"}, workspace
    )
    assert spec.metadata["published"] is True
    first.release(workspace, "succeeded")

    second = _runner(tmp_path, forge, "second")
    again = second.acquire("TASK-000103")

    assert again.branch == "task/TASK-000103"
    assert (again.path / "feature.txt").read_text() == "first attempt\n"
    assert again.head() == spec.metadata["commit"]
    # The next attempt adds to the same line, so the never-forced push goes through.
    (again.path / "feature.txt").write_text("second attempt\n")
    (retry,) = await _agent(second)._commit_evidence(
        {"publicId": "TASK-000103", "title": "Work"}, {"id": "run-2"}, again
    )
    assert retry.metadata["published"] is True
    assert _run_git(forge, "rev-parse", "refs/heads/task/TASK-000103") == retry.metadata["commit"]


async def test_a_task_the_forge_has_no_branch_for_is_cut_from_the_base(
    forge: Path, tmp_path: Path
) -> None:
    pool = _runner(tmp_path, forge, "only")
    workspace = pool.acquire("TASK-000104")

    assert workspace.head() == _run_git(forge, "rev-parse", "refs/heads/main")
    assert not workspace.reused
