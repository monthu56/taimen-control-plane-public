"""Авто-ревью демона (control_plane_agent.review): политика из окружения,
текст задачи ревью, разбор вердикта."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from control_plane_agent.review import (
    ReviewPolicy,
    build_review_task,
    parse_verdict,
    published_commit,
    review_fields,
    review_policy_from_env,
    summary_of,
)
from control_plane_agent.workspace import ExecutionWorkspacePool


@dataclass
class Spec:
    type: str
    name: str = ""
    uri: str | None = None
    content: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


TASK = {
    "id": "11111111-1111-1111-1111-111111111111",
    "publicId": "TASK-000100",
    "title": "BO-05g Runner-адаптер сервиса bidops",
    "typeKey": "coding-task",
    "priority": "high",
    "workspaceId": "33333333-3333-3333-3333-333333333333",
    "description": "Сделать адаптер. Идемпотентно.",
}


def test_policy_is_off_without_reviewer() -> None:
    assert review_policy_from_env({}) is None
    assert review_policy_from_env({"CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL": "  "}) is None


def test_policy_reads_reviewer_types_and_base() -> None:
    policy = review_policy_from_env(
        {
            "CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL": "p-1",
            "CONTROL_PLANE_AGENT_REVIEW_TASK_TYPES": "coding-task, ops",
            "CONTROL_PLANE_AGENT_REVIEW_BASE": "develop",
        }
    )
    assert policy == ReviewPolicy(
        "p-1", frozenset({"coding-task", "ops"}), "code-review", "develop"
    )
    assert policy.applies_to(TASK)
    assert not policy.applies_to({**TASK, "typeKey": "analysis"})


def test_only_a_published_commit_counts() -> None:
    unpublished = Spec("commit", metadata={"branch": "task/T", "commit": "abc", "published": False})
    report = Spec("report", content={"summary": "done"})
    assert published_commit([report, unpublished]) is None
    published = Spec("commit", metadata={"branch": "task/T", "commit": "abc", "published": True})
    assert published_commit([report, published]) == published.metadata


def test_review_task_names_branch_commit_base_and_requirements() -> None:
    policy = ReviewPolicy("reviewer-1")
    spec = build_review_task(TASK, {"branch": "task/TASK-000100", "commit": "deadbeef"}, policy)

    assert spec["type_key"] == "code-review"
    assert spec["assignee_id"] == "reviewer-1"
    assert spec["workspace_id"] == TASK["workspaceId"]
    assert spec["priority"] == "high"
    assert spec["title"].startswith("Ревью TASK-000100:")
    body = spec["description"]
    assert "task/TASK-000100" in body and "deadbeef" in body and "origin/main" in body
    assert "merge-base" in body
    assert "ВЕРДИКТ: approved" in body and "ВЕРДИКТ: changes_requested" in body
    assert "Сделать адаптер. Идемпотентно." in body


def test_long_requirements_are_cut_with_a_pointer_back() -> None:
    spec = build_review_task(
        {**TASK, "description": "x" * 10_000}, {"branch": "b", "commit": "c"}, ReviewPolicy("r")
    )
    assert "описание обрезано" in spec["description"]
    assert "TASK-000100" in spec["description"]


@pytest.mark.parametrize(
    ("summary", "verdict"),
    [
        ("ВЕРДИКТ: approved\n\nВсё сходится.", "approved"),
        ("**ВЕРДИКТ: changes_requested**\nДва замечания.", "changes_requested"),
        ("VERDICT: Changes Requested\nsee below", "changes_requested"),
        ("Отчёт без вердикта", None),
        ("", None),
        (None, None),
    ],
)
def test_verdict_is_parsed_from_the_summary(summary: str | None, verdict: str | None) -> None:
    got, _ = parse_verdict(summary)
    assert got == verdict


def test_notes_keep_the_review_without_the_verdict_line() -> None:
    verdict, notes = parse_verdict("ВЕРДИКТ: approved\n\nПроверено: тесты зелёные.\nЗамечаний нет.")
    assert verdict == "approved"
    assert notes == "Проверено: тесты зелёные.\nЗамечаний нет."


def test_summary_is_taken_from_the_report_artifact() -> None:
    assert (
        summary_of([Spec("commit"), Spec("report", content={"summary": "ВЕРДИКТ: approved"})])
        == "ВЕРДИКТ: approved"
    )
    assert summary_of([Spec("report", content={})]) is None


def test_policy_mode_defaults_to_agent_and_reads_human() -> None:
    assert review_policy_from_env({"CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL": "p-1"}).mode == "agent"
    policy = review_policy_from_env(
        {
            "CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL": "p-1",
            "CONTROL_PLANE_AGENT_REVIEW_MODE": "Human",
        }
    )
    assert policy is not None and policy.human


def test_unknown_review_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="CONTROL_PLANE_AGENT_REVIEW_MODE"):
        review_policy_from_env(
            {
                "CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL": "p-1",
                "CONTROL_PLANE_AGENT_REVIEW_MODE": "x",
            }
        )


def test_human_review_task_asks_for_an_approval_not_a_verdict_line() -> None:
    policy = ReviewPolicy("human-1", mode="human")
    spec = build_review_task(TASK, {"branch": "task/TASK-000100", "commit": "deadbeef"}, policy)

    assert spec["assignee_id"] == "human-1"
    body = spec["description"]
    assert "проверяет человек" in body and "gate-approval" in body
    assert "ВЕРДИКТ: approved" not in body
    assert "task/TASK-000100" in body and "Сделать адаптер. Идемпотентно." in body


def test_review_is_created_by_type_key_so_the_newest_active_version_applies() -> None:
    """Демон называет тип только ключом: сервер берёт свежую active-версию, и
    ревью получает исходы approval из code-review v3 (CP-ADR-0061) без
    перенастройки демона."""
    policy = ReviewPolicy("human-1", mode="human")
    spec = build_review_task(TASK, {"branch": "task/TASK-000100", "commit": "deadbeef"}, policy)

    assert spec["type_key"] == "code-review"
    assert "type_version" not in spec and "type_id" not in spec
    assert "ядро само закроет задачу" in spec["description"]


class _ApprovalClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.approvals: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []

    async def request_approval(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            from control_plane_client.errors import ControlPlaneError

            raise ControlPlaneError("forbidden", "no approvals.manage", status=403)
        self.approvals.append(kwargs)
        return {"id": "ap-1"}

    async def add_task_comment(self, task_ref: str, *, body: str) -> dict[str, Any]:
        self.comments.append((task_ref, body))
        return {}


async def test_human_mode_opens_one_gate_approval_on_the_review_task() -> None:
    from control_plane_agent.main import Agent

    agent = object.__new__(Agent)
    agent.client = _ApprovalClient()
    review = {"id": "rev-1", "publicId": "TASK-000101"}
    commit = {"branch": "task/TASK-000100", "commit": "deadbeef"}

    await agent._request_review_approval(
        review, TASK, commit, ReviewPolicy("human-1", mode="human")
    )

    (call,) = agent.client.approvals
    assert call["task_ref"] == "rev-1"
    assert call["assigned_principal_id"] == "human-1"
    assert call["gate"] is True
    assert call["idempotency_key"] == "code-review-approval:rev-1"
    assert "deadbeef" in call["comment"]


async def test_failed_review_approval_is_reported_on_the_review_task() -> None:
    from control_plane_agent.main import Agent

    agent = object.__new__(Agent)
    agent.client = _ApprovalClient(fail=True)
    review = {"id": "rev-1", "publicId": "TASK-000101"}

    await agent._request_review_approval(
        review, TASK, {"branch": "b", "commit": "c"}, ReviewPolicy("human-1", mode="human")
    )

    ((ref, body),) = agent.client.comments
    assert ref == "rev-1" and "forbidden" in body


def test_review_carries_what_to_merge_where_in_custom_fields() -> None:
    """The review type's approval outcome reads these (``$.task.customFields``)."""
    commit = {
        "branch": "task/TASK-000100",
        "commit": "deadbeef",
        "repository": "https://forge.example/org/repo.git",
        "targetBranch": "master",
        "published": True,
    }
    spec = build_review_task(TASK, commit, ReviewPolicy("r"))
    assert spec["custom_fields"] == {
        "repository": "https://forge.example/org/repo.git",
        "branch": "task/TASK-000100",
        "commit": "deadbeef",
        "targetBranch": "master",
    }


def test_review_fields_fall_back_to_the_policy_base_and_omit_the_unknown() -> None:
    fields = review_fields({"branch": "b", "commit": "c"}, ReviewPolicy("r", base_branch="dev"))
    assert fields == {"branch": "b", "commit": "c", "targetBranch": "dev"}


class _CheckpointClient:
    async def create_checkpoint(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        return {}


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
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=origin,
            check=True,
            capture_output=True,
        )
    return origin


async def test_commit_evidence_says_where_the_branch_lives_and_where_it_goes(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    """Repository (as a shareable URL) and target branch travel with the commit."""
    from control_plane_agent.main import Agent

    origin = origin_with_forge
    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces", push_remote="forge")
    workspace = pool.acquire("TASK-000100")
    (workspace.path / "feature.txt").write_text("work\n")

    agent = object.__new__(Agent)
    agent.client = _CheckpointClient()
    agent.workspaces = pool
    (spec,) = await agent._commit_evidence(
        {"publicId": "TASK-000100", "title": "Work"}, {"id": "run-1"}, workspace
    )

    assert spec.metadata["published"] is True
    assert spec.metadata["repository"] == "https://forge.invalid/org/repo.git"
    assert spec.metadata["targetBranch"] == pool.base_branch
    assert "token" not in str(spec.metadata)
    assert build_review_task(TASK, spec.metadata, ReviewPolicy("r"))["custom_fields"] == {
        "repository": "https://forge.invalid/org/repo.git",
        "branch": workspace.branch,
        "commit": spec.metadata["commit"],
        "targetBranch": pool.base_branch,
    }


def _add_branch(repo: Path, name: str) -> None:
    subprocess.run(["git", "branch", name], cwd=repo, check=True, capture_output=True)


def _add_remote(repo: Path, name: str, url: str) -> None:
    subprocess.run(["git", "remote", "add", name, url], cwd=repo, check=True, capture_output=True)


async def test_commit_evidence_does_not_share_a_remote_nobody_else_can_reach(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    """A push remote that is a local path: published, but no repository to merge from."""
    from control_plane_agent.main import Agent

    origin = origin_with_forge
    _add_remote(origin, "local", str(tmp_path / "forge.git"))
    pool = ExecutionWorkspacePool(origin, tmp_path / "workspaces", push_remote="local")
    workspace = pool.acquire("TASK-000101")
    (workspace.path / "feature.txt").write_text("work\n")

    agent = object.__new__(Agent)
    agent.client = _CheckpointClient()
    agent.workspaces = pool
    (spec,) = await agent._commit_evidence(
        {"publicId": "TASK-000101", "title": "Work"}, {"id": "run-1"}, workspace
    )

    assert spec.metadata["published"] is True
    assert "repository" not in spec.metadata
    assert str(tmp_path) not in str(spec.metadata)
    assert spec.metadata["targetBranch"] == pool.base_branch
    # The review then carries no repository: its merge fails visibly
    # (unresolved_expression) instead of merging from a path on this host.
    fields = build_review_task(TASK, spec.metadata, ReviewPolicy("r"))["custom_fields"]
    assert "repository" not in fields
    assert fields["targetBranch"] == pool.base_branch


async def test_commit_evidence_without_a_push_remote_says_nothing_about_merging(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    from control_plane_agent.main import Agent

    pool = ExecutionWorkspacePool(origin_with_forge, tmp_path / "workspaces")
    workspace = pool.acquire("TASK-000102")
    (workspace.path / "feature.txt").write_text("work\n")

    agent = object.__new__(Agent)
    agent.client = _CheckpointClient()
    agent.workspaces = pool
    (spec,) = await agent._commit_evidence(
        {"publicId": "TASK-000102", "title": "Work"}, {"id": "run-1"}, workspace
    )

    assert spec.metadata["published"] is False
    assert "repository" not in spec.metadata
    assert "targetBranch" not in spec.metadata


async def test_work_on_a_feature_branch_goes_back_into_it(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    """customFields.baseBranch: the copy is cut from it and the merge targets it."""
    from control_plane_agent.main import Agent

    origin = origin_with_forge
    # The forge URL is unreachable here, so the feature branch the mirror
    # already holds is what the copy is cut from (fetch fallback).
    _add_branch(origin, "feature/x")
    task = {**TASK, "customFields": {"baseBranch": "feature/x"}}
    agent = object.__new__(Agent)
    agent.client = _CheckpointClient()
    agent.workspaces = ExecutionWorkspacePool(origin, tmp_path / "workspaces", push_remote="forge")

    workspace = await agent._open_workspace(task, {"id": "run-1"})
    assert workspace is not None
    assert workspace.base_branch == "feature/x"
    (workspace.path / "feature.txt").write_text("work\n")
    (spec,) = await agent._commit_evidence(task, {"id": "run-1"}, workspace)

    assert spec.metadata["targetBranch"] == "feature/x"
    review = build_review_task(task, spec.metadata, ReviewPolicy("r"))
    assert review["custom_fields"]["targetBranch"] == "feature/x"
    assert "целевая ветка origin/feature/x" in review["description"]


async def test_a_task_on_a_missing_base_branch_does_not_get_a_copy(
    origin_with_forge: Path, tmp_path: Path
) -> None:
    from control_plane_agent.main import Agent
    from control_plane_agent.workspace import WorkspaceError

    agent = object.__new__(Agent)
    agent.client = _CheckpointClient()
    agent.workspaces = ExecutionWorkspacePool(origin_with_forge, tmp_path / "workspaces")
    task = {**TASK, "customFields": {"baseBranch": "feature/nope"}}

    with pytest.raises(WorkspaceError, match="base branch feature/nope does not exist"):
        await agent._open_workspace(task, {"id": "run-1"})


class _ReviewClient:
    """Refuses custom fields the way a review type without the merge fields does."""

    def __init__(self, refuse: bool) -> None:
        self.refuse = refuse
        self.created: list[dict[str, Any]] = []

    async def create_task(self, **spec: Any) -> dict[str, Any]:
        from control_plane_client import ControlPlaneError

        if self.refuse and "custom_fields" in spec:
            raise ControlPlaneError("custom_fields_invalid", "additional properties", status=422)
        self.created.append(spec)
        return {"id": "r", "publicId": "TASK-000200"}


@pytest.mark.parametrize("refuse", [False, True])
async def test_review_is_created_even_if_its_type_refuses_the_merge_fields(refuse: bool) -> None:
    from control_plane_agent.main import Agent

    commit = {"branch": "task/TASK-000100", "commit": "abc1234", "published": True}
    spec = build_review_task(TASK, commit, ReviewPolicy("r"))
    agent = object.__new__(Agent)
    agent.client = _ReviewClient(refuse)

    review = await agent._create_review(spec)

    assert review["publicId"] == "TASK-000200"
    (created,) = agent.client.created
    if refuse:
        assert "custom_fields" not in created
    else:
        assert created["custom_fields"]["branch"] == "task/TASK-000100"


class _CoderClient:
    """Enough of the client for the coder side of auto-review."""

    def __init__(self, task_type: dict[str, Any] | None) -> None:
        self.task_type = task_type
        self.created: list[dict[str, Any]] = []
        self.type_reads = 0

    async def get_task_type(self, type_id: str) -> dict[str, Any]:
        from control_plane_client import ControlPlaneError

        self.type_reads += 1
        if self.task_type is None:
            raise ControlPlaneError("forbidden", "no task_types.read", status=403)
        return self.task_type

    async def create_task(self, **spec: Any) -> dict[str, Any]:
        self.created.append(spec)
        return {"id": "rev-1", "publicId": "TASK-000101"}

    async def add_task_relation(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}


def _coder_agent(task_type: dict[str, Any] | None) -> Any:
    from control_plane_agent.main import Agent

    agent = object.__new__(Agent)
    agent.client = _CoderClient(task_type)
    agent.review_policy = ReviewPolicy("reviewer-1")
    agent._completion_work = {}
    return agent


PUBLISHED = [
    Spec("commit", metadata={"branch": "task/TASK-000100", "commit": "c", "published": True})
]
TYPED_TASK = {**TASK, "typeId": "type-1"}


async def test_no_review_from_the_daemon_when_the_type_declares_work_after_completion() -> None:
    """CP-ADR-0061, amendment 2026-09-25: core filed it at completion already."""
    agent = _coder_agent({"id": "type-1", "completionSchema": {"onComplete": {"actions": []}}})

    await agent._request_review(TYPED_TASK, PUBLISHED)
    await agent._request_review(TYPED_TASK, PUBLISHED)

    assert agent.client.created == []
    assert agent.client.type_reads == 1  # versions are immutable: read once


@pytest.mark.parametrize("task_type", [{"id": "type-1", "completionSchema": {}}, None])
async def test_the_daemon_still_files_the_review_for_a_type_that_declares_nothing(
    task_type: dict[str, Any] | None,
) -> None:
    """A type without the section — or one the runner cannot read — keeps the
    behaviour from before, until the packs are moved over."""
    agent = _coder_agent(task_type)

    await agent._request_review(TYPED_TASK, PUBLISHED)

    (created,) = agent.client.created
    assert created["type_key"] == "code-review"
