"""The daemon configured by its agent's revision (CP-ADR-0073 §8, declarative-agents D006).

A principal bound to an agent reads its description with ``GET /agents/me``
and works by the current revision: which work it takes, which executor with
which parameters and instructions, which working copy with which neighbours,
whether its work goes to review, which skills it runs itself, how long a run
may take to drain. Environment variables keep what belongs to the host and
not to the agent — paths, binaries, local logs, the credential — and, for a
principal that is no agent, the whole configuration as before (the env mode
of local debugging).

The revision is fixed for the life of the process. Between runs the daemon
reads ``/agents/me`` again; a newer revision ends the process with
:data:`EXIT_REVISION_CHANGED` once the current run is over, and whoever
placed it starts it again on the new one. A process never mixes two
revisions: ``agentRevisionId`` of every run it starts is the one it was
built from.

Nothing here talks to the executor: this module turns a spec into the
arguments the daemon and its parts already take.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from control_plane_agent.review import (
    DEFAULT_REVIEW_TYPE,
    DEFAULT_REVIEWED_TYPES,
    REVIEW_MODES,
    ReviewPolicy,
)
from control_plane_agent.skills import (
    ENV_AUDIENCES,
    ENV_CONCURRENCY,
    ENV_HTTP_ORIGINS,
    ENV_LOCAL_PACKAGES,
    ENV_MCP_ORIGINS,
    ENV_PROTOCOLS,
    SkillExecutor,
    executor_from_environment,
)
from control_plane_agent.workspace import (
    ExecutionWorkspacePool,
    Neighbour,
    WorkspaceError,
    public_remote_url,
)
from control_plane_client import ControlPlaneClient, NotFoundError

logger = logging.getLogger("control_plane_agent.revision")

#: The agent's revision changed: restart this process on the new one
#: (``EX_TEMPFAIL``). The run in flight was finished first.
EXIT_REVISION_CHANGED = 75
#: The configuration cannot be used — a spec the daemon cannot honour, or an
#: environment missing what the host must provide.
EXIT_MISCONFIGURED = 2

#: ``CONTROL_PLANE_AGENT_CONFIG``: ``auto`` (default) — the revision when the
#: principal is an agent, the environment otherwise; ``revision`` — an agent
#: is required; ``env`` — the environment, for a principal that is no agent.
CONFIG_MODES = ("auto", "revision", "env")
ENV_CONFIG_MODE = "CONTROL_PLANE_AGENT_CONFIG"
#: Where bare mirrors of the revision's repositories live on this host.
ENV_MIRRORS = "CONTROL_PLANE_AGENT_MIRRORS"
#: ``placement.drainSeconds`` when the spec does not say (the run limit).
DEFAULT_DRAIN_SECONDS = 14_400

#: Skill settings a revision owns. The host keeps the rest of
#: ``CONTROL_PLANE_SKILLS_*`` (isolation, private hosts, stdio MCP servers).
_SKILL_ENV = {
    "protocols": ENV_PROTOCOLS,
    "local": ENV_LOCAL_PACKAGES,
    "httpOrigins": ENV_HTTP_ORIGINS,
    "mcpOrigins": ENV_MCP_ORIGINS,
    "audiences": ENV_AUDIENCES,
}


class RevisionError(ValueError):
    """The revision describes something this daemon cannot run."""


@dataclass(frozen=True)
class AgentRevision:
    """One revision of the caller's agent, as ``GET /agents/me`` returned it."""

    key: str
    revision: int
    revision_id: str
    spec_hash: str
    spec: Mapping[str, Any]
    status: str
    state: str
    # Resolved by the core from ``work.workspace`` (an id or a slug).
    workspace_id: str | None = None

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> AgentRevision:
        revision = body["revision"]
        return cls(
            key=str(body["key"]),
            revision=int(revision["revision"]),
            revision_id=str(revision["id"]),
            spec_hash=str(revision["specHash"]),
            spec=dict(revision.get("spec") or {}),
            status=str(body.get("status") or "active"),
            state=str(body.get("state") or "running"),
            workspace_id=str(body["workspaceId"]) if body.get("workspaceId") else None,
        )

    @property
    def label(self) -> str:
        return f"{self.key}@{self.revision}"

    @property
    def retired(self) -> bool:
        return self.status == "retired"

    @property
    def stopped(self) -> bool:
        return self.state == "stopped"

    def section(self, name: str) -> dict[str, Any]:
        value = self.spec.get(name)
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def executor_kind(self) -> str | None:
        kind = self.section("executor").get("kind")
        return str(kind) if kind else None

    @property
    def executor_params(self) -> dict[str, Any]:
        return dict(self.section("executor").get("params") or {})

    @property
    def instructions(self) -> str:
        return str(self.section("executor").get("instructions") or "")


async def my_agent(client: ControlPlaneClient) -> AgentRevision | None:
    """The caller's agent, or None when its principal is not bound to one."""
    try:
        body = await client.get_my_agent()
    except NotFoundError:
        return None
    return AgentRevision.from_body(body)


def config_mode(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    mode = (values.get(ENV_CONFIG_MODE) or "auto").strip().lower()
    if mode not in CONFIG_MODES:
        raise RevisionError(
            f"{ENV_CONFIG_MODE}={mode!r}: expected one of {', '.join(CONFIG_MODES)}"
        )
    return mode


# -- what the daemon takes and how it finishes -----------------------------------


@dataclass(frozen=True)
class RevisionSettings:
    """The daemon's own arguments from a revision (``Agent(**settings.agent_kwargs())``)."""

    workspace_id: str | None = None
    project_id: str | None = None
    include_subprojects: bool = False
    only_assigned: bool = True
    task_types: frozenset[str] = field(default_factory=frozenset)
    review_policy: ReviewPolicy | None = None
    review_type: str = DEFAULT_REVIEW_TYPE
    drain_seconds: float | None = DEFAULT_DRAIN_SECONDS

    def agent_kwargs(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "include_subprojects": self.include_subprojects,
            "only_assigned": self.only_assigned,
            "task_types": self.task_types,
            "review_policy": self.review_policy,
            "review_type": self.review_type,
            "drain_seconds": self.drain_seconds,
        }


def settings_of(revision: AgentRevision) -> RevisionSettings:
    """``work``, ``workingCopy.review`` and ``placement.drainSeconds`` of a revision.

    Defaults are the schema's, not the env mode's: ``onlyAssigned`` is true
    unless the spec says otherwise — an agent takes only work meant for it.
    """
    work = revision.section("work")
    policy, review_type = review_policy_of(revision)
    project = work.get("project")
    return RevisionSettings(
        workspace_id=revision.workspace_id,
        project_id=str(project) if project else None,
        include_subprojects=bool(work.get("includeSubprojects", False)),
        only_assigned=bool(work.get("onlyAssigned", True)),
        task_types=frozenset(str(t) for t in work.get("taskTypes") or []),
        review_policy=policy,
        review_type=review_type,
        drain_seconds=drain_seconds_of(revision),
    )


def drain_seconds_of(revision: AgentRevision) -> float | None:
    """How long a stop may wait for the run in flight; None — as long as it takes.

    An agent with ``placement: none`` is placed by nobody, so nobody drains
    it either: it keeps the old behaviour of finishing the run.
    """
    placement = revision.spec.get("placement")
    if placement == "none":
        return None
    if not isinstance(placement, Mapping):
        return float(DEFAULT_DRAIN_SECONDS)
    return float(placement.get("drainSeconds", DEFAULT_DRAIN_SECONDS))


def review_policy_of(revision: AgentRevision) -> tuple[ReviewPolicy | None, str]:
    """The auto-review policy (``review.py``) and the review type this agent recognises.

    No ``review`` or ``mode: none`` — no review requested. A mode that needs a
    reviewer without one is a :class:`RevisionError`: the env mode would have
    silently skipped review, and a revision is explicit about wanting it.
    """
    review = revision.section("workingCopy").get("review")
    if not isinstance(review, Mapping):
        return None, DEFAULT_REVIEW_TYPE
    review_type = str(review.get("taskType") or DEFAULT_REVIEW_TYPE)
    mode = str(review.get("mode") or "agent")
    if mode == "none":
        return None, review_type
    if mode not in REVIEW_MODES:
        raise RevisionError(f"workingCopy.review.mode {mode!r} is not one of human, agent, none")
    reviewer = str(review.get("reviewer") or "").strip()
    if not reviewer:
        raise RevisionError(f"workingCopy.review.mode {mode} needs a reviewer")
    types = frozenset(str(t) for t in review.get("taskTypes") or DEFAULT_REVIEWED_TYPES)
    return (
        ReviewPolicy(
            reviewer_principal_id=reviewer,
            reviewed_types=types,
            review_type=review_type,
            base_branch=str(review.get("base") or "main"),
            mode=mode,
        ),
        review_type,
    )


# -- skills ----------------------------------------------------------------------


def skills_environ(revision: AgentRevision, environ: Mapping[str, str]) -> dict[str, str] | None:
    """``CONTROL_PLANE_SKILLS_*`` as the revision sets them; None without ``skills``.

    What the spec owns replaces the host's value even when the spec leaves it
    out: a skill protocol configured on the host must not run for an agent
    whose description does not name it.
    """
    skills = revision.section("skills")
    if not skills:
        return None
    values = {k: v for k, v in environ.items() if k not in {*_SKILL_ENV.values(), ENV_CONCURRENCY}}
    for name, variable in _SKILL_ENV.items():
        items = skills.get(name) or []
        if items:
            values[variable] = ",".join(str(item) for item in items)
    if "concurrency" in skills:
        values[ENV_CONCURRENCY] = str(int(skills["concurrency"]))
    return values


def skills_of(
    revision: AgentRevision, client: ControlPlaneClient, environ: Mapping[str, str] | None = None
) -> SkillExecutor | None:
    values = skills_environ(revision, os.environ if environ is None else environ)
    if values is None:
        return None
    try:
        return executor_from_environment(client, values)
    except ValueError as exc:
        raise RevisionError(f"skills: {exc}") from exc


# -- working copy ----------------------------------------------------------------


def workspace_pool_of(
    revision: AgentRevision, environ: Mapping[str, str] | None = None
) -> ExecutionWorkspacePool | None:
    """The working copies of ``workingCopy``; None when the revision has none.

    Repositories are named by URL in a spec, and copies are cut from bare
    mirrors on this host (``CONTROL_PLANE_AGENT_MIRRORS``, by default
    ``<worktree root>/.mirrors``): a mirror the host already keeps is used,
    a missing one is cloned. A value that is a directory on this host is used
    as it is — local debugging and tests. Where the copies live stays the
    host's (``CONTROL_PLANE_AGENT_WORKTREE_ROOT``).
    """
    values = os.environ if environ is None else environ
    copy = revision.section("workingCopy")
    if not copy:
        return None
    root = Path(
        values.get("CONTROL_PLANE_AGENT_WORKTREE_ROOT")
        or Path.home() / ".control-plane-agent" / "worktrees"
    ).expanduser()
    mirrors = Path(values.get(ENV_MIRRORS) or root / ".mirrors").expanduser()
    origin = mirror(str(copy["repository"]), mirrors)
    publish = bool(copy.get("publish", True))
    push_remote = ""
    if publish:
        if _has_remote(origin, "origin"):
            push_remote = "origin"
        else:
            logger.warning("publish requested, but %s has no remote to publish to", origin.name)
    neighbours = [
        Neighbour(str(name), mirror(str(url), mirrors))
        for name, url in (copy.get("neighbours") or {}).items()
    ]
    superproject = mirror(str(copy["superproject"]), mirrors) if copy.get("superproject") else None
    try:
        return ExecutionWorkspacePool(
            origin,
            root,
            base_ref=str(copy.get("baseRef") or "HEAD"),
            keep_on_success=values.get("CONTROL_PLANE_AGENT_KEEP_WORKSPACES") == "1",
            max_workspaces=int(values.get("CONTROL_PLANE_AGENT_MAX_WORKSPACES", "8")),
            push_remote=push_remote,
            repo_dir=str(copy.get("directory") or ""),
            neighbours=neighbours,
            superproject=superproject,
            superproject_remote=(
                "origin" if superproject is not None and _has_remote(superproject, "origin") else ""
            ),
        )
    except WorkspaceError as exc:
        raise RevisionError(f"workingCopy: {exc}") from exc


def mirror(repository: str, mirrors: Path) -> Path:
    """The local repository copies of ``repository`` are cut from.

    A local directory is itself. A URL maps to ``<mirrors>/<name>.git`` by the
    last segment of its path; an existing mirror must have that URL as its
    ``origin`` — two repositories of one name are a conflict to resolve on
    the host, not a guess — and a missing one is cloned bare.
    """
    local = Path(repository).expanduser()
    if "://" not in repository and local.is_dir():
        return local
    name = urlsplit(repository).path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    if not name:
        raise RevisionError(f"repository {_shown(repository)} names no repository")
    path = mirrors / f"{name}.git"
    if path.is_dir():
        current = _git_output(path, "remote", "get-url", "origin")
        if _same_repository(current, repository):
            return path
        raise RevisionError(
            f"mirror {name}.git on this host is of {_shown(current or '(no origin)')}, "
            f"not {_shown(repository)}"
        )
    mirrors.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--bare", "--quiet", repository, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RevisionError(f"could not mirror {_shown(repository)}: exit {result.returncode}")
    logger.info("mirrored %s", _shown(repository))
    return path


def _same_repository(left: str | None, right: str) -> bool:
    def norm(url: str) -> str:
        return url.strip().rstrip("/").removesuffix(".git")

    return left is not None and norm(left) == norm(right)


def _shown(url: str) -> str:
    """A URL safe for a log line: no credentials, no local path."""
    return public_remote_url(url) or "(a local repository)"


def _has_remote(repository: Path, name: str) -> bool:
    return _git_output(repository, "remote", "get-url", name) is not None


def _git_output(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


__all__ = [
    "CONFIG_MODES",
    "EXIT_MISCONFIGURED",
    "EXIT_REVISION_CHANGED",
    "AgentRevision",
    "RevisionError",
    "RevisionSettings",
    "config_mode",
    "drain_seconds_of",
    "mirror",
    "my_agent",
    "review_policy_of",
    "settings_of",
    "skills_environ",
    "skills_of",
    "workspace_pool_of",
]
