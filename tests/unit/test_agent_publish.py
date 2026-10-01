"""Publishing task branches and sharing a queue between replicas (universal-runner U007).

Every push is checked before git is asked: the remote must be the address
the configuration names, and the host's publish hook must allow the target —
a refusal is ``publish_target_rejected`` and pushes nothing. A push that
failed is kept in the replica's unpublished list and retried at the start of
a cycle, with a growing pause, until a limit gives it up to a person.
Candidates of one priority are shuffled, the ones this replica holds a copy
of first; unassigned work is not taken by an agent that takes
its own queue.
"""

import json
import logging
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from control_plane_agent.main import (
    WIP_TRAILER,
    Agent,
    _refused_for_task,
    order_candidates,
    warn_without_publish_hook,
)
from control_plane_agent.publish import (
    ENV_PUBLISH_HOOK,
    HOOK_EXIT_TEMPFAIL,
    LEDGER_NAME,
    RETRY_BASE_SECONDS,
    RETRY_MAX_AGE_SECONDS,
    RETRY_MAX_FAILURES,
    RETRY_MAX_SECONDS,
    CommandPublishHook,
    PublishHookUnavailable,
    PublishRejected,
    PublishTarget,
    Unpublished,
    UnpublishedLedger,
    hook_from_environment,
    publish_branch,
    same_repository,
)
from control_plane_agent.revision import AgentRevision, settings_of, workspace_pool_of
from control_plane_agent.workspace import (
    REMOTE_TIMEOUT_SECONDS,
    ExecutionWorkspacePool,
    WorkspaceBusyError,
)
from control_plane_client import ControlPlaneError, NotEligibleError, PermissionDeniedError

TASK = {"publicId": "TASK-000700", "title": "Work"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def forge(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "initial")
    forge = tmp_path / "forge.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(forge))
    return forge


def _pool(tmp_path: Path, forge: Path, name: str = "one", **kwargs: Any) -> ExecutionWorkspacePool:
    mirror = tmp_path / f"{name}.git"
    if not mirror.exists():
        _git(tmp_path, "clone", "-q", "--bare", str(forge), str(mirror))
    return ExecutionWorkspacePool(
        mirror,
        tmp_path / f"{name}-workspaces",
        base_ref="main",
        push_remote="origin",
        **kwargs,
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


class RecordingHook:
    def __init__(self, refusal: str | None = None) -> None:
        self.refusal = refusal
        self.targets: list[PublishTarget] = []

    def check(self, target: PublishTarget) -> str | None:
        self.targets.append(target)
        return self.refusal


class UnavailableHook:
    """A hook that cannot check now (the forge answers 5xx) until ``up`` is set."""

    def __init__(self, reason: str = "GitHub answered 502") -> None:
        self.reason = reason
        self.up = False
        self.targets: list[PublishTarget] = []

    def check(self, target: PublishTarget) -> str | None:
        self.targets.append(target)
        if not self.up:
            raise PublishHookUnavailable(self.reason)
        return None


class _Client:
    def __init__(self, *, comments_fail: bool = False) -> None:
        self.checkpoints: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.comments_fail = comments_fail

    async def create_checkpoint(self, run_id: str, *, kind: str, data: dict[str, Any]) -> Any:
        self.checkpoints.append({"kind": kind, "data": data})
        return {}

    async def add_task_comment(self, task_ref: str, *, body: str, **_: Any) -> Any:
        if self.comments_fail:
            raise ControlPlaneError("unavailable", "the core is down", status=503)
        self.comments.append((task_ref, body))
        return {}


def _agent(pool: ExecutionWorkspacePool, hook: Any = None) -> Agent:
    return Agent(_Client(), None, workspaces=pool, publish_hook=hook)  # type: ignore[arg-type]


def _hook_script(tmp_path: Path, body: str, name: str = "hook.py") -> list[str]:
    script = tmp_path / name
    script.write_text(body)
    return [sys.executable, str(script)]


# -- the hook command ------------------------------------------------------------


def test_the_hook_command_gets_the_target_on_stdin_and_allows_with_exit_0(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    argv = _hook_script(
        tmp_path, f"import sys, pathlib; pathlib.Path({str(seen)!r}).write_text(sys.stdin.read())"
    )
    target = PublishTarget("https://forge.example/org/repo", "task/TASK-1", "abc", "repo")

    assert CommandPublishHook(argv).check(target) is None
    assert json.loads(seen.read_text()) == {
        "url": "https://forge.example/org/repo",
        "branch": "task/TASK-1",
        "commit": "abc",
        "repositoryKey": "repo",
    }


def test_a_refusal_is_the_first_line_of_stdout_one_line_without_local_paths(
    tmp_path: Path,
) -> None:
    argv = _hook_script(
        tmp_path,
        "print()\nprint('  org/repo is   public; see /home/user/x.log  ')\n"
        "print('second line')\nraise SystemExit(3)",
    )
    reason = CommandPublishHook(argv).check(PublishTarget("u", "b", "c"))

    assert reason == "org/repo is public; see <path>"


def test_a_refusal_without_words_names_the_exit_code(tmp_path: Path) -> None:
    argv = _hook_script(tmp_path, "raise SystemExit(4)")
    assert CommandPublishHook(argv).check(PublishTarget("u", "b", "c")) == (
        "the publish hook refused (exit 4)"
    )


def test_a_long_refusal_is_bounded(tmp_path: Path) -> None:
    argv = _hook_script(tmp_path, "print('x' * 5000)\nraise SystemExit(1)")
    reason = CommandPublishHook(argv).check(PublishTarget("u", "b", "c"))
    assert reason is not None and len(reason) == 300


def test_exit_75_is_not_a_refusal_but_a_check_that_could_not_be_made(tmp_path: Path) -> None:
    """The forge answered 5xx or the network failed (review of U008): ask again later."""
    argv = _hook_script(
        tmp_path,
        "print('GitHub answered 502; see /home/user/x.log')\n"
        f"raise SystemExit({HOOK_EXIT_TEMPFAIL})",
    )
    with pytest.raises(PublishHookUnavailable) as caught:
        CommandPublishHook(argv).check(PublishTarget("u", "b", "c"))
    assert caught.value.reason == "GitHub answered 502; see <path>"


def test_exit_75_without_words_names_the_exit_code(tmp_path: Path) -> None:
    argv = _hook_script(tmp_path, "raise SystemExit(75)")
    with pytest.raises(PublishHookUnavailable) as caught:
        CommandPublishHook(argv).check(PublishTarget("u", "b", "c"))
    assert caught.value.reason == "the publish hook could not check now (exit 75)"


@pytest.mark.parametrize("code", [1, 2, 74, 76, 255])
def test_every_other_nonzero_exit_still_refuses(tmp_path: Path, code: int) -> None:
    argv = _hook_script(tmp_path, f"print('no')\nraise SystemExit({code})")
    assert CommandPublishHook(argv).check(PublishTarget("u", "b", "c")) == "no"


def test_a_hook_that_does_not_answer_refuses(tmp_path: Path) -> None:
    argv = _hook_script(tmp_path, "import time; time.sleep(5)")
    reason = CommandPublishHook(argv, timeout=0.2).check(PublishTarget("u", "b", "c"))
    assert reason == "the publish hook did not answer in 0.2s"


def test_a_hook_that_cannot_start_refuses(tmp_path: Path) -> None:
    reason = CommandPublishHook([str(tmp_path / "missing")]).check(PublishTarget("u", "b", "c"))
    assert reason is not None and reason.startswith("the publish hook could not be started")
    assert str(tmp_path) not in reason


def test_the_hook_comes_from_the_environment() -> None:
    assert hook_from_environment({}) is None
    assert hook_from_environment({ENV_PUBLISH_HOOK: "   "}) is None
    hook = hook_from_environment({ENV_PUBLISH_HOOK: "selfdev-publish-check --catalog 'a b'"})
    assert isinstance(hook, CommandPublishHook)
    assert hook.argv == ["selfdev-publish-check", "--catalog", "a b"]
    with pytest.raises(ValueError):
        hook_from_environment({ENV_PUBLISH_HOOK: "check 'unbalanced"})
    with pytest.raises(ValueError):
        CommandPublishHook([])


# -- the target check ------------------------------------------------------------


def test_a_refused_target_publishes_nothing(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge, publish_url=str(forge), repository_key="repo")
    workspace = pool.acquire("TASK-000700")
    (workspace.path / "work.txt").write_text("work\n")
    sha = workspace.commit("TASK-000700: work")
    hook = RecordingHook("org/repo is public")

    result = publish_branch(pool, workspace.branch, hook)

    assert (result.outcome, result.reason) == ("rejected", "org/repo is public")
    assert _in_forge(forge, workspace.branch) is None
    assert hook.targets == [PublishTarget(str(forge), workspace.branch, str(sha), "repo")]


def test_a_hook_that_cannot_check_now_is_a_failed_push_not_a_refusal(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    workspace = pool.acquire("TASK-000706")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000706: work")
    hook = UnavailableHook("GitHub answered 503 for https://bot:secret@github.com/o/r")

    result = publish_branch(pool, workspace.branch, hook)

    assert (result.outcome, result.reason) == (
        "failed",
        "GitHub answered 503 for https://github.com/o/r",
    )
    assert len(hook.targets) == 1
    assert _in_forge(forge, workspace.branch) is None


def test_an_allowed_target_is_pushed(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    workspace = pool.acquire("TASK-000701")
    (workspace.path / "work.txt").write_text("work\n")
    sha = workspace.commit("TASK-000701: work")

    assert publish_branch(pool, workspace.branch, RecordingHook()).outcome == "published"
    assert _in_forge(forge, workspace.branch) == sha


def test_a_remote_that_is_not_the_configured_repository_is_refused_before_the_hook(
    tmp_path: Path, forge: Path
) -> None:
    """The mirror's push URL was changed on the host: the catalog decides, not the mirror."""
    pool = _pool(tmp_path, forge, publish_url="https://forge.example/org/repo.git")
    workspace = pool.acquire("TASK-000702")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000702: work")
    hook = RecordingHook()

    result = publish_branch(pool, workspace.branch, hook)

    assert result.outcome == "rejected"
    assert "not the repository the catalog names" in result.reason
    assert hook.targets == []
    assert _in_forge(forge, workspace.branch) is None


def test_without_a_configured_address_the_hook_gets_the_remote_without_credentials(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    _git(pool.origin, "remote", "set-url", "origin", "https://bot:secret@forge.invalid/org/r.git")
    _git(pool.origin, "remote", "set-url", "--push", "origin", str(forge))
    workspace = pool.acquire("TASK-000703")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000703: work")
    _git(pool.origin, "remote", "set-url", "--push", "origin", "https://bot:secret@x.invalid/o/r")
    hook = RecordingHook("no")

    publish_branch(pool, workspace.branch, hook)

    assert [t.url for t in hook.targets] == ["https://x.invalid/o/r"]


def test_a_pool_that_publishes_nowhere_stays_local(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    pool.push_remote = ""
    hook = RecordingHook()
    assert publish_branch(pool, "task/TASK-000704", hook).outcome == "local"
    assert hook.targets == []


def test_a_missing_branch_is_a_failed_push(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    assert publish_branch(pool, "task/TASK-000705", None).outcome == "failed"


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("https://github.com/Org/Repo.git", "https://github.com/org/repo", True),
        ("https://github.com/org/repo/", "https://github.com/org/repo.git", True),
        ("https://bot:t@github.com/org/repo", "https://github.com/org/repo", True),
        ("https://github.com/org/repo", "https://github.com/org/other", False),
        ("https://github.com/org/repo", "https://gitlab.com/org/repo", False),
        ("", "", False),
        ("/srv/git/repo.git", "/srv/git/repo", True),
    ],
)
def test_same_repository(left: str, right: str, same: bool) -> None:
    assert same_repository(left, right) is same


# -- the daemon: hand-in, WIP, retries ---------------------------------------------


async def test_a_refused_hand_in_raises_and_records_the_head_unpublished(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    agent = _agent(pool, RecordingHook("private: false"))
    workspace = pool.acquire("TASK-000710")
    (workspace.path / "work.txt").write_text("work\n")

    with pytest.raises(PublishRejected) as caught:
        await agent._commit_evidence(TASK, {"id": "run-1"}, workspace)

    assert caught.value.reason == "private: false"
    assert _in_forge(forge, workspace.branch) is None
    [checkpoint] = agent.client.checkpoints  # type: ignore[attr-defined]
    assert checkpoint["data"]["published"] is False
    assert checkpoint["data"]["publishRejected"] is True
    # Refused is not retried: nothing in the unpublished list.
    assert agent.unpublished is not None and agent.unpublished.entries() == []


async def test_a_hand_in_the_hook_cannot_check_now_is_kept_to_retry(
    tmp_path: Path, forge: Path
) -> None:
    """Exit 75 of the hook at hand-in: not ``publish_target_rejected`` but the unpublished list."""
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    clock = _Clock()
    hook = CommandPublishHook(
        _hook_script(tmp_path, "print('GitHub answered 502')\nraise SystemExit(75)")
    )
    allowing = CommandPublishHook(_hook_script(tmp_path, "raise SystemExit(0)", "allow.py"))
    agent = Agent(_Client(), None, workspaces=pool, publish_hook=hook, clock=clock)  # type: ignore[arg-type]
    workspace = pool.acquire("TASK-000718")
    (workspace.path / "work.txt").write_text("work\n")

    (spec,) = await agent._commit_evidence(TASK, {"id": "run-1"}, workspace)

    assert spec.metadata["published"] is False
    [checkpoint] = agent.client.checkpoints  # type: ignore[attr-defined]
    assert "publishRejected" not in checkpoint["data"]
    assert _in_forge(forge, workspace.branch) is None
    assert agent.unpublished is not None
    [entry] = agent.unpublished.entries()
    assert (entry.task, entry.commit, entry.failures) == (
        "TASK-000718",
        spec.metadata["commit"],
        1,
    )
    assert entry.reason == "GitHub answered 502"

    # Still unavailable in the next cycle: kept, counted.
    await agent._publish_pending()
    [entry] = agent.unpublished.entries()
    assert entry.failures == 2
    assert _in_forge(forge, workspace.branch) is None

    # The forge is back: the hook allows and the retry publishes.
    agent.publish_hook = allowing
    clock.now = entry.retry_at
    await agent._publish_pending()

    assert _in_forge(forge, workspace.branch) == spec.metadata["commit"]
    assert agent.unpublished.entries() == []


async def test_wip_the_hook_cannot_check_now_is_kept_to_retry(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    agent = _agent(pool, UnavailableHook())
    workspace = pool.acquire("TASK-000719")
    (workspace.path / "half.txt").write_text("half\n")

    data = await agent._save_wip(workspace, "why")

    assert data is not None
    assert (data["wip"], data["published"]) == (True, False)
    assert "publishRejected" not in data
    assert agent.unpublished is not None
    [entry] = agent.unpublished.entries()
    assert (entry.branch, entry.commit) == (workspace.branch, data["head"])


async def test_a_hook_that_cannot_check_for_ever_is_given_up_after_the_limit(
    tmp_path: Path, forge: Path
) -> None:
    """Exit 75 on every retry counts toward the U007 limit, as a failed push does."""
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    hook = UnavailableHook()
    clock = _Clock()
    client = _Client()
    agent = Agent(client, None, workspaces=pool, publish_hook=hook, clock=clock)  # type: ignore[arg-type]
    ledger = agent.unpublished
    assert ledger is not None
    workspace = pool.acquire("TASK-000720")
    (workspace.path / "work.txt").write_text("work\n")

    await agent._commit_evidence(TASK, {"id": "run-1"}, workspace)
    while ledger.entries():
        clock.now = ledger.entries()[0].retry_at
        await agent._publish_pending()

    assert len(hook.targets) == RETRY_MAX_FAILURES
    [(task_ref, body)] = client.comments
    assert task_ref == "TASK-000720"
    assert f"was not published after {RETRY_MAX_FAILURES} attempt(s)" in body
    assert "GitHub answered 502" in body
    assert _in_forge(forge, workspace.branch) is None


async def test_an_unpublished_commit_goes_out_in_the_next_cycle(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    clock = _Clock()
    agent = Agent(_Client(), None, workspaces=pool, clock=clock)  # type: ignore[arg-type]
    workspace = pool.acquire("TASK-000711")
    (workspace.path / "work.txt").write_text("work\n")
    away = forge.with_name("forge-away.git")
    os.rename(forge, away)  # the forge is unreachable

    (spec,) = await agent._commit_evidence(TASK, {"id": "run-1"}, workspace)

    assert spec.metadata["published"] is False
    assert agent.unpublished is not None
    [entry] = agent.unpublished.entries()
    assert (entry.task, entry.branch, entry.commit) == (
        "TASK-000711",
        workspace.branch,
        spec.metadata["commit"],
    )
    assert (pool.root / LEDGER_NAME).exists()

    # Still unreachable: the entry stays for the next cycle.
    await agent._publish_pending()
    assert len(agent.unpublished.entries()) == 1

    os.rename(away, forge)
    clock.now = agent.unpublished.entries()[0].retry_at  # its pause is over
    await agent._publish_pending()

    assert _in_forge(forge, workspace.branch) == spec.metadata["commit"]
    assert agent.unpublished.entries() == []


async def test_a_retry_pushes_the_branch_as_it_is_now(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    workspace = pool.acquire("TASK-000712")
    (workspace.path / "one.txt").write_text("1\n")
    first = workspace.commit("TASK-000712: one")
    assert agent.unpublished is not None
    agent.unpublished.add(Unpublished("TASK-000712", workspace.branch, str(first)))
    (workspace.path / "two.txt").write_text("2\n")
    second = workspace.commit("TASK-000712: two")

    await agent._publish_pending()

    assert _in_forge(forge, workspace.branch) == second


async def test_a_retry_drops_what_cannot_be_helped(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    workspace = pool.acquire("TASK-000713")
    (workspace.path / "work.txt").write_text("work\n")
    sha = str(workspace.commit("TASK-000713: work"))
    agent = _agent(pool, RecordingHook("renamed"))
    ledger = agent.unpublished
    assert ledger is not None
    ledger.add(Unpublished("TASK-000713", workspace.branch, sha))
    ledger.add(Unpublished("TASK-000714", "task/TASK-000714", sha))  # branch gone
    ledger.add(Unpublished("TASK-000715", "task/TASK-000715", sha, "elsewhere"))  # no such pool

    await agent._publish_pending()

    assert ledger.entries() == []
    assert _in_forge(forge, workspace.branch) is None


async def test_a_blocked_run_saves_its_work_as_wip_and_publishes_it(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    workspace = pool.acquire("TASK-000716")
    (workspace.path / "half.txt").write_text("half\n")

    data = await agent._save_wip(workspace, "why")

    assert data is not None
    assert (data["wip"], data["published"], data["workspaceKey"]) == (True, True, "TASK-000716")
    assert _in_forge(forge, workspace.branch) == data["head"]
    assert WIP_TRAILER in _git(forge, "log", "-1", "--format=%B", data["head"])
    assert not workspace.is_dirty


async def test_nothing_to_save_writes_no_wip(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    workspace = pool.acquire("TASK-000717")

    assert await agent._save_wip(workspace, "why") is None
    assert _in_forge(forge, workspace.branch) is None


async def test_a_continued_run_that_adds_nothing_hands_in_a_result_over_the_wip(
    tmp_path: Path, forge: Path
) -> None:
    """WIP is never the result (FR-022): a commit of the task goes on top of it."""
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    workspace = pool.acquire("TASK-000718")
    (workspace.path / "half.txt").write_text("half\n")
    wip = await agent._save_wip(workspace, "why")
    assert wip is not None
    pool.release(workspace, "failed")

    again = pool.acquire("TASK-000718")
    (spec,) = await agent._commit_evidence(TASK, {"id": "run-2"}, again)

    result = spec.metadata["commit"]
    assert result != wip["head"]
    assert _git(pool.origin, "rev-parse", f"{result}^") == wip["head"]
    message = _git(pool.origin, "log", "-1", "--format=%B", result)
    assert WIP_TRAILER not in message
    assert message.startswith("TASK-000700: Work")
    assert _in_forge(forge, again.branch) == result
    # The tree is the WIP's: nothing was added, nothing was lost.
    assert _git(pool.origin, "rev-parse", f"{result}^{{tree}}") == _git(
        pool.origin, "rev-parse", f"{wip['head']}^{{tree}}"
    )


async def test_a_continued_run_that_adds_work_hands_in_its_own_commit(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    workspace = pool.acquire("TASK-000718")
    (workspace.path / "half.txt").write_text("half\n")
    wip = await agent._save_wip(workspace, "why")
    assert wip is not None
    (workspace.path / "rest.txt").write_text("rest\n")

    (spec,) = await agent._commit_evidence(TASK, {"id": "run-2"}, workspace)

    result = spec.metadata["commit"]
    assert _git(pool.origin, "rev-parse", f"{result}^") == wip["head"]
    assert WIP_TRAILER not in _git(pool.origin, "log", "-1", "--format=%B", result)
    assert _git(pool.origin, "show", f"{result}:rest.txt") == "rest"


async def test_a_continued_run_without_saved_work_hands_in_nothing(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000719")
    assert await _agent(pool)._commit_evidence(TASK, {"id": "run-1"}, workspace) == []


# -- the unpublished list -----------------------------------------------------------


def test_the_list_keeps_one_entry_per_branch(tmp_path: Path) -> None:
    ledger = UnpublishedLedger(tmp_path / "root")
    ledger.add(Unpublished("TASK-1", "task/TASK-1", "a"))
    ledger.add(Unpublished("TASK-1", "task/TASK-1", "b"))
    ledger.add(Unpublished("TASK-1", "task/TASK-1", "c", "other-repo"))

    assert [(e.commit, e.repository_key) for e in ledger.entries()] == [
        ("b", ""),
        ("c", "other-repo"),
    ]
    ledger.discard("", "task/TASK-1")
    ledger.discard("", "task/TASK-1")  # twice is the same as once
    assert [e.commit for e in ledger.entries()] == ["c"]


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"items": 3}', '{"items": [1, {"task": "T"}, {"task": 1}]}'],
)
def test_a_list_that_cannot_be_read_is_empty(tmp_path: Path, content: str) -> None:
    (tmp_path / LEDGER_NAME).write_text(content)
    ledger = UnpublishedLedger(tmp_path)
    assert ledger.entries() == []
    ledger.add(Unpublished("TASK-1", "task/TASK-1", "a"))
    assert len(ledger.entries()) == 1


def test_entries_with_wrong_fields_are_skipped(tmp_path: Path) -> None:
    good = {"task": "TASK-1", "branch": "task/TASK-1", "commit": "a", "repositoryKey": ""}
    bad = [{**good, "commit": ""}, {**good, "repositoryKey": None}, {**good, "branch": 1}]
    (tmp_path / LEDGER_NAME).write_text(json.dumps({"items": [good, *bad]}))
    assert [e.task for e in UnpublishedLedger(tmp_path).entries()] == ["TASK-1"]


def test_parallel_additions_are_all_kept(tmp_path: Path) -> None:
    ledgers = [UnpublishedLedger(tmp_path) for _ in range(8)]
    threads = [
        threading.Thread(target=ledger.add, args=(Unpublished(f"T-{i}", f"task/T-{i}", "a"),))
        for i, ledger in enumerate(ledgers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(e.task for e in ledgers[0].entries()) == [f"T-{i}" for i in range(8)]


# -- a copy continued by another replica -----------------------------------------------


def test_a_stale_branch_catches_up_with_its_published_head(tmp_path: Path, forge: Path) -> None:
    """Worked here, continued elsewhere, taken here again: resume from the newer head."""
    here = _pool(tmp_path, forge, "here")
    workspace = here.acquire("TASK-000720")
    (workspace.path / "one.txt").write_text("1\n")
    workspace.commit("TASK-000720: one")
    assert publish_branch(here, workspace.branch, None).published
    here.release(workspace, "failed")

    elsewhere = _pool(tmp_path, forge, "elsewhere")
    theirs = elsewhere.acquire("TASK-000720")
    (theirs.path / "two.txt").write_text("2\n")
    newer = theirs.commit("TASK-000720: two")
    assert publish_branch(elsewhere, theirs.branch, None).published

    again = here.acquire("TASK-000720")  # the copy is still here, clean
    assert again.head() == newer
    assert (again.path / "two.txt").exists()
    here.release(again, "succeeded")  # the copy goes, the branch stays

    (theirs.path / "three.txt").write_text("3\n")
    newest = theirs.commit("TASK-000720: three")
    assert publish_branch(elsewhere, theirs.branch, None).published
    assert here.acquire("TASK-000720").head() == newest  # no copy: the branch moves


def test_a_dirty_or_diverged_copy_is_not_moved(tmp_path: Path, forge: Path) -> None:
    here = _pool(tmp_path, forge, "here")
    workspace = here.acquire("TASK-000721")
    (workspace.path / "one.txt").write_text("1\n")
    mine = workspace.commit("TASK-000721: one")
    assert publish_branch(here, workspace.branch, None).published
    here.release(workspace, "failed")

    elsewhere = _pool(tmp_path, forge, "elsewhere")
    theirs = elsewhere.acquire("TASK-000721")
    (theirs.path / "two.txt").write_text("2\n")
    theirs.commit("TASK-000721: two")
    assert publish_branch(elsewhere, theirs.branch, None).published

    (workspace.path / "dirty.txt").write_text("uncommitted\n")
    dirty = here.acquire("TASK-000721")
    assert dirty.head() == mine
    here.release(dirty, "failed")

    (workspace.path / "dirty.txt").unlink()
    (workspace.path / "local.txt").write_text("local\n")
    diverged = workspace.commit("TASK-000721: local")
    assert here.acquire("TASK-000721").head() == diverged


def test_reopen_takes_the_copy_as_it_is(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    assert pool.reopen("TASK-000722") is None
    assert pool.reopen("../escape") is None
    workspace = pool.acquire("TASK-000722")
    with pytest.raises(WorkspaceBusyError):
        pool.reopen("TASK-000722")
    pool.release(workspace, "failed")

    reopened = pool.reopen("TASK-000722")
    assert reopened is not None
    assert reopened.base_commit == _git(forge, "rev-parse", "main")
    pool.release(reopened, "failed")
    shutil.rmtree(pool.path_for("TASK-000722"))
    assert pool.reopen("TASK-000722") is None


# -- choosing work --------------------------------------------------------------------


def _items(*specs: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"publicId": key, "priority": priority} for key, priority in specs]


def test_candidates_keep_priority_and_prefer_own_copies() -> None:
    items = _items(("A1", "high"), ("A2", "high"), ("A3", "high"), ("B1", "low"), ("B2", "low"))
    mine = {"A3", "B2"}

    ordered = order_candidates(items, own=lambda i: i["publicId"] in mine, rng=random.Random(1))

    keys = [i["publicId"] for i in ordered]
    assert keys[0] == "A3"
    assert set(keys[1:3]) == {"A1", "A2"}
    assert keys[3:] == ["B2", "B1"]


def test_candidates_of_one_priority_are_shuffled() -> None:
    items = _items(*((f"T{i}", "high") for i in range(8)))
    orders = {
        tuple(i["publicId"] for i in order_candidates(items, own=lambda _: False, rng=rng))
        for rng in (random.Random(seed) for seed in range(10))
    }
    assert len(orders) > 1
    assert order_candidates([], own=lambda _: True, rng=random.Random()) == []


def test_unassigned_work_is_not_taken_by_an_agent_of_its_own_queue(tmp_path: Path) -> None:
    agent = Agent(object(), None, only_assigned=True)  # type: ignore[arg-type]
    items = [
        {"publicId": "T1", "assigneeId": "p-1"},
        {"publicId": "T2", "assigneeId": None},
        {"publicId": "T3"},
    ]
    assert [i["publicId"] for i in agent._candidates(items)] == ["T1"]
    free = Agent(object(), None)  # type: ignore[arg-type]
    assert len(free._candidates(items)) == 3


def test_own_copies_are_what_the_replica_has_a_container_of(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000730")
    agent = _agent(pool)
    assert agent._has_copy({"publicId": "TASK-000730"})
    assert not agent._has_copy({"publicId": "TASK-000731"})
    for strange in ("", None, "../x", ".locks", "a/b"):
        assert not agent._has_copy({"publicId": strange})
    pool.release(workspace, "failed")


def _revision(spec: dict[str, Any]) -> AgentRevision:
    return AgentRevision(
        key="coder",
        revision=1,
        revision_id="11111111-1111-1111-1111-111111111111",
        spec_hash="sha256:0",
        spec=spec,
        status="active",
        state="running",
    )


def test_a_catalog_agent_never_takes_unassigned_work() -> None:
    catalog = {"repositoryField": "repositoryKey", "repositories": {"a": {"url": "/x"}}}
    spec = {"work": {"onlyAssigned": False}, "workingCopy": catalog}
    assert settings_of(_revision(spec)).only_assigned is True
    # The one-repository form keeps what its description says.
    legacy = {"work": {"onlyAssigned": False}, "workingCopy": {"repository": "/x"}}
    assert settings_of(_revision(legacy)).only_assigned is False


# -- review of attempt #1: what must never be pushed, and how often ---------------------


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _advance_main(pool: ExecutionWorkspacePool) -> str:
    """Move ``main`` of the mirror past the forge's, as a stray write could."""
    workspace = pool.acquire("TASK-000749")
    (workspace.path / "stray.txt").write_text("stray\n")
    sha = str(workspace.commit("TASK-000749: stray"))
    pool.release(workspace, "failed")
    _git(pool.origin, "update-ref", "refs/heads/main", sha)
    return sha


@pytest.mark.parametrize(
    ("task", "branch"),
    [
        ("TASK-000740", "main"),  # not a task branch at all
        ("TASK-000740", "task/TASK-000741"),  # another task's branch
        ("main", "main"),
        ("../TASK-000740", "task/../TASK-000740"),
        ("", "main"),
    ],
)
async def test_a_pending_entry_goes_only_to_the_branch_of_its_task(
    tmp_path: Path, forge: Path, task: str, branch: str
) -> None:
    """The list lives on a volume the executor can write: it names nothing to push."""
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    before = _in_forge(forge, "main")
    _advance_main(pool)
    other = pool.acquire("TASK-000741")
    (other.path / "x.txt").write_text("x\n")
    other.commit("TASK-000741: x")
    hook = RecordingHook()
    agent = _agent(pool, hook)
    assert agent.unpublished is not None
    (pool.root / LEDGER_NAME).write_text(
        json.dumps({"items": [{"task": task, "branch": branch, "commit": "a"}]})
    )

    await agent._publish_pending()

    assert hook.targets == []
    assert _in_forge(forge, "main") == before
    assert _in_forge(forge, "task/TASK-000741") is None
    assert agent.unpublished.entries() == []


def test_the_pool_pushes_only_task_branches(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    before = _in_forge(forge, "main")
    _advance_main(pool)
    assert publish_branch(pool, "main", None).outcome == "failed"
    assert pool.push_branch("main") == "not a task branch"
    assert pool.push_branch("task/../main") == "not a task branch"
    assert _in_forge(forge, "main") == before


def test_every_push_address_must_be_the_configured_one(tmp_path: Path, forge: Path) -> None:
    """``git push`` goes to every push URL, so every one is checked, not the first."""
    elsewhere = tmp_path / "elsewhere.git"
    _git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(forge))
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(elsewhere))
    workspace = pool.acquire("TASK-000742")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000742: work")
    hook = RecordingHook()

    result = publish_branch(pool, workspace.branch, hook)

    assert result.outcome == "rejected"
    assert hook.targets == []
    assert _in_forge(forge, workspace.branch) is None
    assert _in_forge(elsewhere, workspace.branch) is None


def test_two_push_addresses_are_refused_without_a_configured_one(
    tmp_path: Path, forge: Path
) -> None:
    elsewhere = tmp_path / "elsewhere.git"
    _git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    pool = _pool(tmp_path, forge)
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(forge))
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(elsewhere))
    workspace = pool.acquire("TASK-000743")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000743: work")

    assert publish_branch(pool, workspace.branch, None).outcome == "rejected"
    assert _in_forge(elsewhere, workspace.branch) is None


def test_credentials_of_the_configured_address_do_not_reach_the_hook(
    tmp_path: Path, forge: Path
) -> None:
    address = "https://bot:secret@forge.invalid/org/r.git"
    pool = _pool(tmp_path, forge, publish_url=address)
    workspace = pool.acquire("TASK-000744")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000744: work")
    _git(pool.origin, "remote", "set-url", "origin", address)
    hook = RecordingHook("no")

    publish_branch(pool, workspace.branch, hook)

    assert [t.url for t in hook.targets] == ["https://forge.invalid/org/r.git"]


def test_the_push_sends_the_commit_the_hook_allowed(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    workspace = pool.acquire("TASK-000745")
    (workspace.path / "one.txt").write_text("1\n")
    checked = workspace.commit("TASK-000745: one")

    class MovesTheBranch:
        def check(self, target: PublishTarget) -> str | None:
            (workspace.path / "two.txt").write_text("2\n")
            workspace.commit("TASK-000745: two")  # moved after it was checked
            return None

    assert publish_branch(pool, workspace.branch, MovesTheBranch()).published
    assert _in_forge(forge, workspace.branch) == checked


def test_git_talking_to_the_forge_never_prompts_and_is_bounded(
    tmp_path: Path, forge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000746")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000746: work")
    seen: list[tuple[list[str], float, dict[str, Any]]] = []

    def hanging(argv: list[str], cwd: Path, timeout: float, **kwargs: Any) -> Any:
        seen.append((list(argv), timeout, kwargs))
        return None  # ran past its timeout and was killed

    monkeypatch.setattr("control_plane_agent.workspace._bounded", hanging)

    assert pool.push_branch(workspace.branch) == (
        f"git push did not finish in {REMOTE_TIMEOUT_SECONDS:g}s"
    )
    assert pool.published_state(workspace.branch) == "unreachable"
    assert [argv[:2] for argv, _, _ in seen] == [["git", "push"], ["git", "fetch"]]
    assert all(timeout > 0 for _, timeout, _ in seen)
    assert all(kw["env"]["GIT_TERMINAL_PROMPT"] == "0" for _, _, kw in seen)


def test_a_hung_forge_is_killed_with_its_transport(tmp_path: Path, forge: Path) -> None:
    """A transport that holds the pipes does not keep a fetch past its bound."""
    pool = _pool(tmp_path, forge, base_fetch_timeout=0.5)
    workspace = pool.acquire("TASK-000759")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000759: work")
    _git(pool.origin, "config", "remote.origin.uploadpack", "sleep 60; git-upload-pack")
    started = time.monotonic()

    assert pool.published_state(workspace.branch) == "unreachable"
    assert time.monotonic() - started < 10


async def test_a_retry_drops_a_branch_rewritten_in_the_forge(tmp_path: Path, forge: Path) -> None:
    """Not a fast-forward any more (a person rebased it): retrying cannot help."""
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    workspace = pool.acquire("TASK-000747")
    (workspace.path / "one.txt").write_text("1\n")
    workspace.commit("TASK-000747: one")
    assert publish_branch(pool, workspace.branch, None).published
    tree = _git(forge, "rev-parse", "main^{tree}")
    rewritten = _git(forge, "commit-tree", tree, "-p", "main", "-m", "rebased by a person")
    _git(forge, "update-ref", f"refs/heads/{workspace.branch}", rewritten)
    (workspace.path / "two.txt").write_text("2\n")
    workspace.commit("TASK-000747: two")
    hook = RecordingHook()
    agent = _agent(pool, hook)
    assert agent.unpublished is not None
    agent.unpublished.add(Unpublished("TASK-000747", workspace.branch, workspace.head()))

    await agent._publish_pending()

    assert hook.targets == []
    assert _in_forge(forge, workspace.branch) == rewritten
    assert agent.unpublished.entries() == []


async def test_a_retry_of_a_branch_the_forge_already_has_drops_it(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000748")
    (workspace.path / "one.txt").write_text("1\n")
    workspace.commit("TASK-000748: one")
    assert publish_branch(pool, workspace.branch, None).published
    hook = RecordingHook()
    agent = _agent(pool, hook)
    assert agent.unpublished is not None
    agent.unpublished.add(Unpublished("TASK-000748", workspace.branch, workspace.head()))

    await agent._publish_pending()

    assert hook.targets == []
    assert agent.unpublished.entries() == []


async def test_a_push_that_keeps_failing_is_given_up_after_the_limit(
    tmp_path: Path, forge: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forge is up but refuses the push (review of attempt #2): not for ever.

    The pause grows, so the hook is not asked every cycle; after
    ``RETRY_MAX_FAILURES`` failures the entry is off the list, the task gets
    a comment with the reason, and nothing is asked any more.
    """
    refuse = forge / "hooks" / "pre-receive"
    refuse.write_text("#!/bin/sh\necho 'branch protected' >&2\nexit 1\n")
    refuse.chmod(0o755)
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    hook = RecordingHook()
    clock = _Clock()
    client = _Client()
    agent = Agent(client, None, workspaces=pool, publish_hook=hook, clock=clock)  # type: ignore[arg-type]
    ledger = agent.unpublished
    assert ledger is not None
    workspace = pool.acquire("TASK-000750")
    (workspace.path / "work.txt").write_text("work\n")

    (spec,) = await agent._commit_evidence(TASK, {"id": "run-1"}, workspace)
    assert spec.metadata["published"] is False
    assert len(hook.targets) == 1

    await agent._publish_pending()  # the next cycle tries once more
    assert len(hook.targets) == 2
    for _ in range(5):
        await agent._publish_pending()  # every ~5 s: nothing asked
    assert len(hook.targets) == 2

    clock.now += RETRY_BASE_SECONDS + 1
    await agent._publish_pending()
    assert len(hook.targets) == 3
    clock.now += RETRY_BASE_SECONDS + 1  # the pause doubled: not yet
    await agent._publish_pending()
    assert len(hook.targets) == 3
    clock.now += RETRY_BASE_SECONDS
    await agent._publish_pending()
    assert len(hook.targets) == 4
    [entry] = ledger.entries()
    assert entry.failures == 4
    assert "pre-receive hook declined" in entry.reason
    assert client.comments == []

    # Alembic's fileConfig in an earlier migration test disables existing loggers.
    monkeypatch.setattr(logging.getLogger("control_plane_agent"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="control_plane_agent"):
        while ledger.entries():
            clock.now = ledger.entries()[0].retry_at
            await agent._publish_pending()

    assert len(hook.targets) == RETRY_MAX_FAILURES
    [(task_ref, body)] = client.comments
    assert task_ref == "TASK-000750"
    assert f"was not published after {RETRY_MAX_FAILURES} attempt(s)" in body
    assert "pre-receive hook declined" in body
    assert str(tmp_path) not in body
    assert "giving up" in caplog.text
    # Given up: no more pushes, no more comments; the branch stays in the mirror.
    for _ in range(3):
        clock.now += RETRY_MAX_SECONDS
        await agent._publish_pending()
    assert len(hook.targets) == RETRY_MAX_FAILURES
    assert len(client.comments) == 1
    assert _in_forge(forge, workspace.branch) is None
    assert pool.branch_head(workspace.branch) == spec.metadata["commit"]


async def test_a_push_failing_for_hours_is_given_up_before_the_count(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    clock = _Clock()
    client = _Client()
    agent = Agent(client, None, workspaces=pool, clock=clock)  # type: ignore[arg-type]
    ledger = agent.unpublished
    assert ledger is not None
    workspace = pool.acquire("TASK-000753")
    (workspace.path / "work.txt").write_text("work\n")
    os.rename(forge, forge.with_name("away.git"))
    await agent._commit_evidence(TASK, {"id": "run-1"}, workspace)

    clock.now += RETRY_MAX_AGE_SECONDS - 1  # the replica was down meanwhile
    await agent._publish_pending()
    [entry] = ledger.entries()
    assert entry.failures == 2
    clock.now = entry.retry_at + 1
    await agent._publish_pending()

    assert ledger.entries() == []
    [(task_ref, body)] = client.comments
    assert task_ref == "TASK-000753"
    assert "after 3 attempt(s): the forge did not answer" in body


async def test_a_given_up_branch_whose_comment_fails_is_still_off_the_list(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000754")
    (workspace.path / "work.txt").write_text("work\n")
    sha = str(workspace.commit("TASK-000754: work"))
    agent = Agent(_Client(comments_fail=True), None, workspaces=pool)  # type: ignore[arg-type]
    ledger = agent.unpublished
    assert ledger is not None
    (pool.root / LEDGER_NAME).write_text(
        json.dumps(
            {
                "items": [
                    {
                        "task": "TASK-000754",
                        "branch": workspace.branch,
                        "commit": sha,
                        "failures": RETRY_MAX_FAILURES - 1,
                    }
                ]
            }
        )
    )
    os.rename(forge, forge.with_name("away.git"))

    await agent._publish_pending()  # the comment fails: the cycle goes on

    assert ledger.entries() == []


def test_the_list_gives_up_an_entry_at_the_limit(tmp_path: Path) -> None:
    ledger = UnpublishedLedger(tmp_path)
    entry = Unpublished("TASK-1", "task/TASK-1", "a", reason="denied")
    for n in range(1, RETRY_MAX_FAILURES):
        recorded = ledger.add(entry, now=100.0 + n)
        assert (recorded.failures, recorded.exhausted(100.0 + n)) == (n, False)
    assert ledger.entries()[0].first_failed_at == 101.0

    last = ledger.add(entry, now=200.0)

    assert (last.failures, last.exhausted(200.0), last.reason) == (
        RETRY_MAX_FAILURES,
        True,
        "denied",
    )
    assert ledger.entries() == []
    # Recorded again, it starts afresh.
    assert ledger.add(entry, now=300.0).failures == 1


def test_the_age_counts_from_the_first_failure(tmp_path: Path) -> None:
    ledger = UnpublishedLedger(tmp_path)
    entry = Unpublished("TASK-1", "task/TASK-1", "a")
    ledger.add(entry, now=1000.0)
    ledger.add(replace(entry, commit="b"), now=1000.0 + RETRY_MAX_AGE_SECONDS - 1)
    assert ledger.entries()[0].first_failed_at == 1000.0

    assert ledger.add(entry, now=1000.0 + RETRY_MAX_AGE_SECONDS).exhausted(
        1000.0 + RETRY_MAX_AGE_SECONDS
    )
    assert ledger.entries() == []


def test_a_first_failure_in_the_future_counts_from_now(tmp_path: Path) -> None:
    """A clock set back, or a forged file, does not make an entry immortal or dead."""
    item = {"task": "TASK-1", "branch": "task/TASK-1", "commit": "a", "failures": 1}
    (tmp_path / LEDGER_NAME).write_text(
        json.dumps({"items": [{**item, "firstFailedAt": 10 * RETRY_MAX_AGE_SECONDS}]})
    )
    ledger = UnpublishedLedger(tmp_path)

    recorded = ledger.add(Unpublished("TASK-1", "task/TASK-1", "a"), now=500.0)

    assert (recorded.failures, recorded.first_failed_at) == (2, 500.0)
    assert not recorded.exhausted(500.0)


def test_the_reason_is_recorded_without_credentials_or_local_paths(tmp_path: Path) -> None:
    ledger = UnpublishedLedger(tmp_path)
    reason = (
        f"fatal: unable to access 'https://bot:s3cret@forge.invalid/o/r.git/' in {tmp_path}/x\n"
        + "y" * 1000
    )
    recorded = ledger.add(Unpublished("TASK-1", "task/TASK-1", "a", reason=reason))
    [entry] = ledger.entries()
    for text in (recorded.reason, entry.reason):
        assert "s3cret" not in text and "bot" not in text
        assert str(tmp_path) not in text
        assert "\n" not in text
        assert len(text) <= 300
        assert "https://forge.invalid/o/r.git/" in text


async def test_an_unreachable_forge_is_retried_with_a_growing_pause(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    clock = _Clock()
    agent = Agent(_Client(), None, workspaces=pool, clock=clock)  # type: ignore[arg-type]
    ledger = agent.unpublished
    assert ledger is not None
    workspace = pool.acquire("TASK-000751")
    (workspace.path / "work.txt").write_text("work\n")
    os.rename(forge, forge.with_name("away.git"))
    await agent._commit_evidence(TASK, {"id": "run-1"}, workspace)
    [entry] = ledger.entries()
    assert (entry.failures, entry.retry_at) == (1, clock.now)

    await agent._publish_pending()
    [entry] = ledger.entries()
    assert (entry.failures, entry.retry_at) == (2, clock.now + RETRY_BASE_SECONDS)
    await agent._publish_pending()
    assert ledger.entries() == [entry]  # not due: untouched

    for _ in range(8):
        clock.now = ledger.entries()[0].retry_at
        await agent._publish_pending()
    [entry] = ledger.entries()
    assert entry.failures == 10
    assert entry.reason == "the forge did not answer"
    assert entry.retry_at - clock.now == RETRY_MAX_SECONDS  # bounded


def test_the_pause_is_kept_in_the_list(tmp_path: Path) -> None:
    ledger = UnpublishedLedger(tmp_path)
    ledger.add(Unpublished("TASK-1", "task/TASK-1", "a"), now=100.0)
    ledger.add(Unpublished("TASK-1", "task/TASK-1", "b"), now=100.0)
    [entry] = UnpublishedLedger(tmp_path).entries()
    assert (entry.commit, entry.failures, entry.retry_at) == ("b", 2, 100.0 + RETRY_BASE_SECONDS)


@pytest.mark.parametrize(
    "extra",
    [
        {"failures": "2"},
        {"failures": -1},
        {"failures": True},
        {"retryAt": "soon"},
        {"retryAt": None},
        {"firstFailedAt": "yesterday"},
        {"firstFailedAt": -5},
        {"firstFailedAt": None},
        {"reason": 42},
        {"reason": None},
        {},
    ],
)
def test_a_pause_with_wrong_fields_is_no_pause(tmp_path: Path, extra: dict[str, Any]) -> None:
    item = {"task": "TASK-1", "branch": "task/TASK-1", "commit": "a", **extra}
    (tmp_path / LEDGER_NAME).write_text(json.dumps({"items": [item]}))
    [entry] = UnpublishedLedger(tmp_path).entries()
    assert (entry.failures, entry.retry_at, entry.first_failed_at, entry.reason) == (
        0,
        0.0,
        0.0,
        "",
    )
    assert not entry.exhausted(10 * RETRY_MAX_AGE_SECONDS)  # an unknown start: only the count


# -- review of attempt #1: what WIP is -------------------------------------------------


async def test_a_copy_the_executor_committed_itself_is_published_but_is_no_wip(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    workspace = pool.acquire("TASK-000752")
    (workspace.path / "own.txt").write_text("own\n")
    own = workspace.commit("TASK-000752: its own commit")

    data = await agent._save_wip(workspace, "why")

    assert data is not None
    assert "wip" not in data
    assert (data["head"], data["published"]) == (own, True)
    assert _in_forge(forge, workspace.branch) == own


def _with_submodule(tmp_path: Path) -> Path:
    """A forge whose main has a submodule ``sub``."""
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    (sub / "s.txt").write_text("s\n")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-qm", "sub")
    seed = tmp_path / "seed-sub"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    _git(seed, "commit", "-qm", "with a submodule")
    forge = tmp_path / "forge-sub.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(forge))
    return forge


async def test_wip_does_not_commit_a_moved_submodule_pointer(tmp_path: Path) -> None:
    forge = _with_submodule(tmp_path)
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    workspace = pool.acquire("TASK-000753")
    pinned = _git(workspace.path, "rev-parse", "HEAD:sub")
    _git(workspace.path, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
    (workspace.path / "sub" / "moved.txt").write_text("moved\n")
    _git(workspace.path / "sub", "add", "-A")
    _git(workspace.path / "sub", "commit", "-qm", "moved")

    assert await agent._save_wip(workspace, "why") is None  # only the pointer moved

    (workspace.path / "half.txt").write_text("half\n")
    data = await agent._save_wip(workspace, "why")

    assert data is not None and data["wip"] is True
    assert _git(forge, "rev-parse", f"{data['head']}:sub") == pinned
    assert _git(forge, "show", f"{data['head']}:half.txt") == "half"


# -- review of attempt #1: the host is told what it did not configure --------------------


def test_a_catalog_without_a_publish_hook_is_warned_about(
    tmp_path: Path, forge: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Alembic's fileConfig in an earlier migration test disables existing loggers.
    monkeypatch.setattr(logging.getLogger("control_plane_agent"), "disabled", False)
    spec = {
        "workingCopy": {
            "repositoryField": "repositoryKey",
            "repositories": {"a": {"url": "https://forge.invalid/o/a.git"}},
        }
    }
    pools = workspace_pool_of(
        _revision(spec), {"CONTROL_PLANE_AGENT_WORKTREE_ROOT": str(tmp_path / "w")}
    )
    with caplog.at_level("WARNING"):
        assert warn_without_publish_hook(pools, None) is True
    assert ENV_PUBLISH_HOOK in caplog.text
    assert warn_without_publish_hook(pools, RecordingHook()) is False
    assert warn_without_publish_hook(_pool(tmp_path, forge), None) is False
    assert warn_without_publish_hook(None, None) is False


def test_a_pause_beyond_the_longest_one_is_not_believed() -> None:
    entry = Unpublished("TASK-1", "task/TASK-1", "a", failures=3, retry_at=100.0)
    assert not entry.due(99.0)
    assert entry.due(100.0)
    far = Unpublished("TASK-1", "task/TASK-1", "a", retry_at=100.0 + 10 * RETRY_MAX_SECONDS)
    assert far.due(100.0)


# -- review of attempt #2: pushed where it was checked, only tasks run here -----------


def test_the_push_goes_to_the_checked_address_not_to_the_remote_read_again(
    tmp_path: Path, forge: Path
) -> None:
    """The mirror's config is shared with the executor's copy: a push URL changed
    between the check and the push does not get the branch."""
    elsewhere = tmp_path / "elsewhere.git"
    _git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    workspace = pool.acquire("TASK-000755")
    (workspace.path / "work.txt").write_text("work\n")
    sha = workspace.commit("TASK-000755: work")

    class SwapsTheRemote:
        def check(self, target: PublishTarget) -> str | None:
            _git(workspace.path, "remote", "set-url", "--push", "origin", str(elsewhere))
            return None

    assert publish_branch(pool, workspace.branch, SwapsTheRemote()).published
    assert _in_forge(forge, workspace.branch) == sha
    assert _in_forge(elsewhere, workspace.branch) is None


def test_a_push_to_no_address_is_a_failure(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000756")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000756: work")
    assert pool.push_branch(workspace.branch, urls=[]) == "no address to push to"
    assert pool.push_branch(workspace.branch, urls=[""]) == "no address to push to"
    assert _in_forge(forge, workspace.branch) is None


def test_a_failed_push_says_why_without_credentials(tmp_path: Path, forge: Path) -> None:
    refuse = forge / "hooks" / "pre-receive"
    refuse.write_text("#!/bin/sh\nexit 1\n")
    refuse.chmod(0o755)
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    workspace = pool.acquire("TASK-000757")
    (workspace.path / "work.txt").write_text("work\n")
    workspace.commit("TASK-000757: work")

    result = publish_branch(pool, workspace.branch, None)

    assert result.outcome == "failed"
    assert "pre-receive hook declined" in result.reason
    assert str(tmp_path) not in result.reason


async def test_a_pending_entry_of_a_task_without_a_copy_here_is_dropped(
    tmp_path: Path, forge: Path
) -> None:
    """The list is on a volume the executor writes: a branch in the mirror
    (which replicas may share) whose task has no copy on this replica is not
    pushed from here."""
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000758")
    (workspace.path / "work.txt").write_text("work\n")
    sha = str(workspace.commit("TASK-000758: work"))
    pool.release(workspace, "succeeded")  # the copy is gone, the branch stays
    assert not pool.has_copy("TASK-000758")
    assert pool.branch_head(workspace.branch) == sha
    hook = RecordingHook()
    agent = _agent(pool, hook)
    assert agent.unpublished is not None
    agent.unpublished.add(Unpublished("TASK-000758", workspace.branch, sha))

    await agent._publish_pending()

    assert hook.targets == []
    assert _in_forge(forge, workspace.branch) is None
    assert agent.unpublished.entries() == []


# -- the disk budget and the unpublished list (review of U007) -----------------


def _committed(pool: ExecutionWorkspacePool, key: str) -> str:
    """A copy of ``key`` with a commit of its own, given back as a failed run leaves it."""
    workspace = pool.acquire(key)
    (workspace.path / "work.txt").write_text(f"{key}\n")
    sha = str(workspace.commit(f"{key}: work"))
    pool.release(workspace, "failed")
    return sha


def _waiting(pool: ExecutionWorkspacePool, key: str, sha: str, repository_key: str = "") -> None:
    """Put the branch of ``key`` on the unpublished list beside the pool's copies."""
    UnpublishedLedger(pool.root).add(Unpublished(key, pool.branch_for(key), sha, repository_key))


def test_the_disk_budget_does_not_prune_a_copy_on_the_unpublished_list(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    _waiting(pool, "TASK-000770", _committed(pool, "TASK-000770"))
    _committed(pool, "TASK-000771")
    last = pool.acquire("TASK-000772")
    pool.max_workspaces = 0

    pool.release(last, "failed")

    assert pool.has_copy("TASK-000770")  # waits to be pushed: kept over the budget
    assert not pool.has_copy("TASK-000771")  # idle and not waiting: pruned
    assert pool.has_copy("TASK-000772")  # the one just given back
    assert pool.branch_head("task/TASK-000771") is not None  # branches are never touched


def test_the_disk_budget_without_a_list_prunes_as_before(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    _committed(pool, "TASK-000773")
    _committed(pool, "TASK-000774")
    pool.max_workspaces = 1

    pool.release(pool.acquire("TASK-000775"), "failed")

    assert not pool.has_copy("TASK-000773")  # the oldest idle one goes
    assert pool.has_copy("TASK-000774")
    assert pool.has_copy("TASK-000775")


def test_entries_naming_no_copy_of_the_pool_keep_nothing(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    _committed(pool, "TASK-000773")
    _committed(pool, "TASK-000774")
    _waiting(pool, "TASK-009999", "a" * 40)  # no copy here
    _waiting(pool, "TASK-000773", "a" * 40, "elsewhere")  # another repository's
    UnpublishedLedger(pool.root).add(Unpublished("TASK-000773", "task/other", "a" * 40))
    pool.max_workspaces = 1

    pool.release(pool.acquire("TASK-000775"), "failed")

    assert not pool.has_copy("TASK-000773")
    assert pool.has_copy("TASK-000774")


def test_an_unreadable_list_prunes_nothing(tmp_path: Path, forge: Path) -> None:
    pool = _pool(tmp_path, forge)
    _committed(pool, "TASK-000773")
    pool.max_workspaces = 0
    # The lock cannot be opened: which copies wait is unknown.
    lock = pool.root / f"{LEDGER_NAME}.lock"
    lock.unlink(missing_ok=True)
    lock.mkdir()

    assert pool.pinned_tasks() is None
    pool.release(pool.acquire("TASK-000775"), "failed")

    assert pool.has_copy("TASK-000773")


@pytest.mark.parametrize("content", ["not json", "[]", '{"items": 3}', ""])
def test_a_list_that_cannot_be_parsed_prunes_nothing(
    tmp_path: Path, forge: Path, content: str
) -> None:
    pool = _pool(tmp_path, forge)
    _waiting(pool, "TASK-000773", _committed(pool, "TASK-000773"))
    _committed(pool, "TASK-000774")
    pool.max_workspaces = 0
    # Broken after the copy was pinned: it may still wait, so none is pruned.
    (pool.root / LEDGER_NAME).write_text(content)

    assert pool.pinned_tasks() is None
    pool.release(pool.acquire("TASK-000775"), "failed")

    assert pool.has_copy("TASK-000773")
    assert pool.has_copy("TASK-000774")


def test_a_list_that_cannot_be_parsed_is_told_from_no_list(tmp_path: Path) -> None:
    ledger = UnpublishedLedger(tmp_path)
    assert ledger.read() == []  # no file: nothing waits
    (tmp_path / LEDGER_NAME).write_text('{"items": []}')
    assert ledger.read() == []  # an empty list: nothing waits either
    (tmp_path / LEDGER_NAME).write_text("{broken")
    assert ledger.read() is None  # unknown
    assert ledger.entries() == []  # the retries start afresh, as before
    ledger.discard("", "task/TASK-1")  # nothing to drop: the file is left as it is
    assert ledger.read() is None


def test_pins_are_the_tasks_of_the_pool_on_the_unpublished_list(
    tmp_path: Path, forge: Path
) -> None:
    pool = _pool(tmp_path, forge)
    assert pool.pinned_tasks() == set()  # no list yet
    ledger = UnpublishedLedger(pool.root)
    ledger.add(Unpublished("TASK-000776", "task/TASK-000776", "a" * 40))
    ledger.add(Unpublished("TASK-000777", "task/TASK-000777", "a" * 40, "elsewhere"))
    ledger.add(Unpublished("TASK-000778", "task/TASK-000779", "a" * 40))  # not its branch
    ledger.add(Unpublished("TASK-000776", "task/TASK-000776", "b" * 40))  # again: one entry

    assert pool.pinned_tasks() == {"TASK-000776"}
    # The list the agent keeps is the one the pool reads.
    agent = _agent(pool)
    assert agent.unpublished is not None
    assert agent.unpublished.path == pool.root / LEDGER_NAME


async def test_a_copy_waiting_on_the_list_outlives_the_budget_and_is_published(
    tmp_path: Path, forge: Path
) -> None:
    """More tasks handed in than the budget holds while one waits: its push still goes."""
    pool = _pool(tmp_path, forge)
    agent = _agent(pool)
    ledger = agent.unpublished
    assert ledger is not None
    sha = _committed(pool, "TASK-000780")
    pool.max_workspaces = 0
    ledger.add(Unpublished("TASK-000780", "task/TASK-000780", sha))
    for key in ("TASK-000781", "TASK-000782"):
        workspace = pool.acquire(key)
        pool.release(workspace, "succeeded")

    assert pool.has_copy("TASK-000780")
    await agent._publish_pending()

    assert _in_forge(forge, "task/TASK-000780") == sha
    assert ledger.entries() == []


class _RecoveryClient(_Client):
    async def get_task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "publicId": task_id}


async def test_restart_recovery_does_not_prune_a_copy_on_the_unpublished_list(
    tmp_path: Path, forge: Path
) -> None:
    """Review of TASK-001185: a replica restarted while the forge is down, with
    more copies than its budget, saves an orphaned run's work; the release
    after it must not prune the copy of a branch still to publish."""
    pool = _pool(tmp_path, forge)
    agent = Agent(_RecoveryClient(), None, workspaces=pool)  # type: ignore[arg-type]
    ledger = agent.unpublished
    assert ledger is not None
    sha = _committed(pool, "TASK-000790")
    ledger.add(Unpublished("TASK-000790", "task/TASK-000790", sha))
    _committed(pool, "TASK-000791")
    orphan = pool.acquire("TASK-000792")
    (orphan.path / "half.txt").write_text("half\n")
    pool.release(orphan, "failed")
    pool.max_workspaces = 0
    away = forge.with_name("away.git")
    os.rename(forge, away)

    elsewhere, record = await agent._save_orphaned_work({"id": "run-1", "taskId": "TASK-000792"})

    assert (elsewhere, record is not None) == (False, True)
    assert pool.has_copy("TASK-000790")  # still waits on the list: kept
    assert not pool.has_copy("TASK-000791")  # idle and not waiting: pruned
    assert pool.has_copy("TASK-000792")  # its WIP waits on the list now
    assert {e.task for e in ledger.entries()} == {"TASK-000790", "TASK-000792"}


# -- the published branch at every push address (review of U007) ---------------


def _bare(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    _git(tmp_path, "init", "-q", "--bare", str(path))
    return path


def test_published_is_same_only_at_every_push_address(tmp_path: Path, forge: Path) -> None:
    second = _bare(tmp_path, "second.git")
    pool = _pool(tmp_path, forge)
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(forge))
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(second))
    workspace = pool.acquire("TASK-000783")
    (workspace.path / "one.txt").write_text("1\n")
    first = str(workspace.commit("TASK-000783: one"))
    assert pool.push_branch(workspace.branch, urls=[str(forge)]) is None

    # The fetch address has it; the second push address does not.
    assert pool.published_state(workspace.branch) == "absent"

    assert pool.push_branch(workspace.branch, urls=[str(second)]) is None
    assert pool.published_state(workspace.branch) == "same"

    (workspace.path / "two.txt").write_text("2\n")
    workspace.commit("TASK-000783: two")
    assert pool.push_branch(workspace.branch, urls=[str(forge)]) is None
    # The fetch address is up to date, the second one is an ancestor behind.
    assert pool.published_state(workspace.branch) == "behind"
    assert _in_forge(second, workspace.branch) == first


def test_one_unreachable_or_diverged_push_address_decides(tmp_path: Path, forge: Path) -> None:
    second = _bare(tmp_path, "second.git")
    pool = _pool(tmp_path, forge)
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(forge))
    _git(pool.origin, "remote", "set-url", "--add", "--push", "origin", str(second))
    workspace = pool.acquire("TASK-000784")
    (workspace.path / "one.txt").write_text("1\n")
    workspace.commit("TASK-000784: one")
    assert pool.push_branch(workspace.branch) is None
    assert pool.published_state(workspace.branch) == "same"

    # Someone else's commit at the second address: a push there cannot succeed.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(second), str(other))
    _git(other, "checkout", "-q", workspace.branch)
    (other / "theirs.txt").write_text("theirs\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "theirs")
    _git(other, "push", "-q", "origin", workspace.branch)
    assert pool.published_state(workspace.branch) == "diverged"

    shutil.rmtree(second)  # the second address does not answer
    assert pool.published_state(workspace.branch) == "unreachable"


def test_a_push_address_that_is_an_option_is_not_asked(
    tmp_path: Path, forge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(tmp_path, forge)
    workspace = pool.acquire("TASK-000785")
    (workspace.path / "one.txt").write_text("1\n")
    workspace.commit("TASK-000785: one")
    assert pool.push_branch(workspace.branch) is None
    monkeypatch.setattr(pool, "push_urls", lambda: [str(forge), "--upload-pack=touch x"])

    assert pool.published_state(workspace.branch) == "unreachable"
    assert not (pool.origin / "x").exists()


async def test_a_retry_is_not_settled_by_the_fetch_address_alone(
    tmp_path: Path, forge: Path
) -> None:
    """Fetched from a read replica that has the branch, pushed to a forge that has not."""
    reader = _bare(tmp_path, "reader.git")
    pool = _pool(tmp_path, forge, publish_url=str(forge))
    _git(pool.origin, "remote", "set-url", "origin", str(reader))
    _git(pool.origin, "remote", "set-url", "--push", "origin", str(forge))
    workspace = pool.acquire("TASK-000786")
    (workspace.path / "one.txt").write_text("1\n")
    sha = str(workspace.commit("TASK-000786: one"))
    _git(pool.origin, "push", "-q", str(reader), f"{workspace.branch}:{workspace.branch}")
    agent = _agent(pool)
    assert agent.unpublished is not None
    agent.unpublished.add(Unpublished("TASK-000786", workspace.branch, sha))

    await agent._publish_pending()

    assert _in_forge(forge, workspace.branch) == sha
    assert agent.unpublished.entries() == []


# -- which refused claims stop the cycle (review of U007) -----------------------


@pytest.mark.parametrize(
    ("error", "for_task"),
    [
        (PermissionDeniedError("permission_denied", "x", details={"resource": "task:1"}), True),
        (NotEligibleError("not_eligible", "x"), True),
        (PermissionDeniedError("not_eligible", "x"), True),
        (PermissionDeniedError("permission_denied", "x", details={"resource": "tenant:1"}), False),
        (PermissionDeniedError("permission_denied", "x"), False),
        (PermissionDeniedError("permission_denied", "x", details=None), False),
        (PermissionDeniedError("permission_denied", "x", details={"resource": None}), False),
        (PermissionDeniedError("permission_denied", "x", details={"resource": ["task:1"]}), False),
        (PermissionDeniedError("permission_denied", "x", details={"resource": ""}), False),
        (PermissionDeniedError("session_owner_mismatch", "x"), False),
        (PermissionDeniedError("principal_not_active", "x"), False),
    ],
)
def test_only_a_refusal_about_the_task_moves_to_the_next_candidate(
    error: PermissionDeniedError, for_task: bool
) -> None:
    assert _refused_for_task(error) is for_task
