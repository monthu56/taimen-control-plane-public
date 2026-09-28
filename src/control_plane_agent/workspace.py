"""Execution workspace: an isolated working copy per task (ADR-0016 §5).

Why this exists: an adapter that works in ``os.getcwd()`` serializes the
process to one task at a time and makes the resulting commit useless as
evidence, because changes of different tasks blend together. Here every task
gets its own git worktree on a deterministic branch ``task/<publicId>``, and
the commit — referenced, never copied (harness-protocol §8) — is what proves
the work happened.

Ownership rule (ADR-0016 §2): the workspace belongs to the process holding the
claim. That is enforced locally by an exclusive lock file per workspace, so a
second process cannot write into the same copy even by mistake.

Restart survivability: nothing here is remembered in memory. The branch and the
workspace key are written to a Run Checkpoint by the caller, and ``acquire()``
of the same key reuses the existing copy — including its uncommitted changes —
instead of creating a second one.

Portability of evidence: absolute local paths never leave this module. What
goes to the Control Plane is the workspace key, the branch and the commit sha;
``assert_portable()`` rejects anything else before it is sent.

Completeness of the copy: a repository that builds against a sibling through a
path dependency (``platform-auth-sdk`` at ``../platform-auth-sdk``) cannot be
built from a copy of itself alone. So a copy is not one directory but a small
container — the working copy next to the neighbours it needs — and each
neighbour is checked out at the revision the SUPERPROJECT pins, never at the
tip of its own branch. Otherwise a green test run says nothing: it was run
against a combination of revisions that exists in no commit anywhere.
"""

import contextlib
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("control_plane_agent.workspace")

CHECKPOINT_KIND = "execution.workspace"
ARTIFACT_TYPE = "commit"
BRANCH_PREFIX = "task/"
DEFAULT_COMMITTER = ("control-plane-agent", "agent@control-plane.local")

Outcome = Literal["succeeded", "failed", "suspended"]

# A workspace key is a task public id; it becomes a directory name and a branch
# name, so anything that could escape the root or confuse git is refused.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Payload guard. The goal is not general-purpose DLP but a hard stop on the two
# classes the task forbids: local filesystem paths and obvious credentials.
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "authorization",
        "credential",
        "key_hash",
        "password",
        "secret",
        "token",
    }
)
_PATH_ROOTS = ("/Users/", "/home/", "/root/", "/private/", "/var/", "/tmp/", "/opt/", "/mnt/")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
# The same roots, as a pattern that matches a path ANYWHERE in free text — for
# redacting messages rather than rejecting payloads. Deliberately anchored on
# known roots instead of "anything with slashes", so a URL or a repo-relative
# path is left alone.
_LOCAL_PATH_RE = re.compile(
    # The lookbehind is not decoration: without it the Windows branch matches
    # "s:/" inside "https://…" and redacts every URL in a message.
    r"(?:(?<![A-Za-z])[A-Za-z]:[\\/][^\s'\"]*"
    r"|(?:/(?:Users|home|root|private|var|tmp|opt|mnt|Volumes))(?:/[^\s'\"]*)?)"
)
_WHOLE_PATH_RE = re.compile(r"^/(?:[^/\s]+/)*[^/\s]*$")
_TOKEN_PREFIXES = ("cp_", "sk-", "ghp_", "github_pat_", "xox", "-----BEGIN")


# A neighbour directory name is also a path inside the container, so it is held
# to the same standard as a workspace key: no escaping, no surprises for git.
_NEIGHBOUR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# "160000 commit <sha>\t<path>" — the gitlink line of ``git ls-tree``.
_GITLINK_MODE = "160000"
# Per-branch git config recording what a task branch was cut from. Kept in the
# repository config rather than in the copy, so it outlives a removed copy and
# goes away with the branch (``git branch -D`` drops the branch's section).
_BASE_BRANCH_KEY = "controlPlaneBase"
_BASE_COMMIT_KEY = "controlPlaneBaseCommit"


class WorkspaceError(RuntimeError):
    """The working copy is not in the state the caller assumed."""


class WorkspaceBusyError(WorkspaceError):
    """Another process already holds this workspace."""


class UnsafePayloadError(ValueError):
    """A payload carries a local path or a credential and must not be sent."""


def assert_portable(data: Any, *, where: str = "payload") -> None:
    """Reject local paths and obvious credentials before they leave the host.

    Checkpoints, artifacts and events are read by other principals in other
    environments, where an absolute path of this machine is at best noise and
    at worst a disclosure (harness-protocol §8, ADR-0016 verification list).
    """
    _walk(data, where)


def _walk(value: Any, where: str, key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _walk(child, f"{where}.{child_key}", str(child_key))
        return
    if isinstance(value, str | bytes):
        _check_string(
            value if isinstance(value, str) else value.decode("utf-8", "replace"), where, key
        )
        return
    if isinstance(value, Sequence):
        for index, child in enumerate(value):
            _walk(child, f"{where}[{index}]")


def _check_string(value: str, where: str, key: str | None) -> None:
    if key is not None and key.lower().replace("-", "_") in _SENSITIVE_KEYS and value:
        raise UnsafePayloadError(f"{where} looks like a credential and must not be sent")
    if value.startswith(_TOKEN_PREFIXES):
        raise UnsafePayloadError(f"{where} looks like a credential and must not be sent")
    if value.startswith("file://") or value.startswith("~/") or _WINDOWS_PATH_RE.match(value):
        raise UnsafePayloadError(f"{where} carries a local path and must not be sent")
    if any(root in value for root in _PATH_ROOTS) or _WHOLE_PATH_RE.match(value):
        raise UnsafePayloadError(f"{where} carries a local path and must not be sent")


def redact_local_paths(text: str) -> str:
    """Replace absolute host paths with a placeholder, keeping the rest intact.

    An error message has two readers with opposite needs: the runner's log wants
    every detail, and durable Control Plane state must carry no path of this
    machine (ADR-0016, harness-protocol §8). Redaction serves the second without
    reducing the message to "something failed" — the git subcommand, the exit
    condition and the remote's own words survive.
    """
    return _LOCAL_PATH_RE.sub("<path>", text)


def public_remote_url(url: str) -> str | None:
    """A remote URL as others may read it, or ``None`` if it cannot be shared.

    Credentials embedded in an http(s) URL (``https://user:token@host/…``) are
    dropped; a local path or ``file://`` remote means nothing off this host and
    is not shared at all (``assert_portable``).
    """
    url = url.strip()
    scheme, sep, rest = url.partition("://")
    if sep and scheme.lower() in ("http", "https", "ssh", "git"):
        authority, slash, path = rest.partition("/")
        _, at, host = authority.rpartition("@")
        if at and scheme.lower() in ("http", "https"):
            authority = host
        url = f"{scheme}://{authority}{slash}{path}"
    if not url:
        return None
    try:
        assert_portable(url, where="repository")
    except UnsafePayloadError:
        return None
    return url


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    # Fixed argv, never a shell: workspace keys are validated before they reach git.
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        # Full detail to the log, redacted detail to the exception: the caller
        # may put this text into a run's failure_reason, which is durable and
        # read elsewhere. Fixing it here rather than at that call site means
        # every future caller inherits the guarantee.
        logger.warning("%s", detail)
        raise WorkspaceError(redact_local_paths(detail))
    return result.stdout.strip()


@dataclass(frozen=True)
class Neighbour:
    """A repository the working copy must be able to build against.

    ``path`` is the submodule path in the superproject AND the directory name
    next to the working copy — they are the same string on purpose, because
    that is what a path dependency like ``../platform-auth-sdk`` names.
    ``origin`` is where copies of it are cut from, normally a bare mirror on
    the runner.
    """

    path: str
    origin: Path

    def __post_init__(self) -> None:
        if not _NEIGHBOUR_RE.match(self.path):
            raise WorkspaceError(f"unsafe neighbour name: {self.path!r}")


def _check_branch_name(name: str) -> None:
    """Refuse a base branch git would not accept or would read as an option.

    The name comes from a task field anyone with write access to the task can
    set, and it ends up in git argv and in a refspec.
    """
    valid = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{name}"], capture_output=True, check=False
    )
    if not name or name.startswith("-") or valid.returncode != 0:
        raise WorkspaceError(f"unsafe base branch name: {name!r}")


def base_branch_of(task: Mapping[str, Any]) -> str | None:
    """The task's own base branch (``customFields.baseBranch``), if it has one.

    An ordinary field of the task type, not a core concept: a task without it
    keeps the pool's default base. A value that is not a string is refused
    rather than ignored — ignoring it would cut the copy from the default base.
    """
    value = (task.get("customFields") or {}).get("baseBranch")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        kind = type(value).__name__
        raise WorkspaceError(f"customFields.baseBranch must be a string, got {kind}")
    return value.strip()


def parse_neighbours(spec: str) -> list[Neighbour]:
    """Read ``name=/path/to/mirror.git`` pairs, comma or whitespace separated.

    A malformed entry raises instead of being skipped: a neighbour silently
    dropped here comes back as a build failure inside the agent's copy, where
    the cause is no longer visible.
    """
    neighbours: list[Neighbour] = []
    for entry in (item for chunk in spec.split(",") for item in chunk.split()):
        name, separator, origin = entry.partition("=")
        if not separator or not origin:
            raise WorkspaceError(f"neighbour must be given as name=origin, got {entry!r}")
        neighbours.append(Neighbour(name, Path(origin).expanduser()))
    return neighbours


@dataclass
class Workspace:
    """One task's working copy. Created and released by the pool."""

    key: str
    branch: str
    path: Path
    base_commit: str
    reused: bool
    # Revision each neighbour was placed at, by directory name. Part of the
    # evidence: it says which combination of revisions the work was done and
    # verified against.
    neighbours: Mapping[str, str] = field(default_factory=dict)
    # Branch the copy was cut from and the work is meant to be merged into: the
    # task's own ``baseBranch`` (a feature branch), or the pool's default.
    base_branch: str = ""
    _lock_fd: int | None = None

    @property
    def container(self) -> Path:
        """Directory holding this copy and its neighbours."""
        return self.path.parent

    @property
    def checkpoint_data(self) -> dict[str, Any]:
        """Durable, portable state of this workspace — no local paths."""
        data: dict[str, Any] = {
            "workspaceKey": self.key,
            "branch": self.branch,
            "baseCommit": self.base_commit,
            "reused": self.reused,
        }
        if self.base_branch:
            data["baseBranch"] = self.base_branch
        if self.neighbours:
            data["neighbours"] = dict(self.neighbours)
        assert_portable(data, where=CHECKPOINT_KIND)
        return data

    @property
    def is_dirty(self) -> bool:
        return bool(_git(self.path, "status", "--porcelain"))

    def head(self) -> str:
        return _git(self.path, "rev-parse", "HEAD")

    def commit(
        self,
        summary: str = "",
        *,
        committer: tuple[str, str] = DEFAULT_COMMITTER,
    ) -> str | None:
        """Commit everything in the copy as evidence. None if nothing changed.

        The message always carries the task public id: a commit that cannot be
        traced back to its task is not evidence.

        An agent that commits inside the copy itself leaves a clean tree but a
        branch that moved past ``base_commit``. That IS the evidence — the
        daemon must publish it, not report "no changes" and leave the branch
        stranded on the runner (seen on the first BidOps smoke run).
        """
        _git(self.path, "add", "-A")
        if not self.is_dirty:
            head = self.head()
            return head if head != self.base_commit else None
        name, email = committer
        message = summary.strip() or f"{self.key}: automated execution"
        if self.key not in message:
            message = f"{message}\n\nTask: {self.key}"
        _git(
            self.path,
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "--no-verify",
            "-m",
            message,
        )
        return self.head()

    def push(self, remote: str = "origin") -> bool:
        """Publish the task branch so the work can be reviewed in the forge.

        Three deliberate limits. Only ``self.branch`` is ever pushed, and it is
        named explicitly on both sides — a runner offers work for review, it
        does not move the branch everyone else builds on. The push is never
        forced: if the remote branch has diverged, that is a person's business
        to resolve, and overwriting it would destroy exactly the review history
        this exists to create. And a failure is reported, not raised — the
        commit is already the evidence, so a forge that is unreachable must not
        turn finished work into a failed run.
        """
        result = subprocess.run(
            ["git", "push", remote, f"refs/heads/{self.branch}:refs/heads/{self.branch}"],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # stderr may name the remote URL and local paths, so it stays in the
            # runner's log and never travels to the Control Plane.
            logger.warning("push of %s failed: %s", self.branch, result.stderr.strip()[:300])
            return False
        return True

    def artifact_uri(self, sha: str) -> str:
        """Reference, not content: the Control Plane is not a file store."""
        return f"git:{sha}"


class ExecutionWorkspacePool:
    """Hands out isolated working copies of one repository, one per task.

    ``origin`` is the repository the copies are made from — a bare clone on a
    runner, a normal checkout in tests. Copies live under ``root`` and are git
    worktrees, so they share the object store and cost little disk each.

    Each task gets a container directory ``root/<key>/`` holding the working
    copy at ``root/<key>/<repo_dir>/`` and, beside it, every configured
    neighbour. The nesting is what makes a path dependency ``../<neighbour>``
    resolve, and it keeps neighbours per task: two tasks running at once cannot
    move the same sibling under each other's feet.

    ``superproject`` is the repository whose submodules pin the neighbours.
    Revisions are read from its tree, so a copy is built against the
    combination of revisions somebody actually committed — not against the tip
    of each neighbour's branch, which is a combination no commit describes.
    """

    def __init__(
        self,
        origin: Path | str,
        root: Path | str,
        *,
        base_ref: str = "HEAD",
        branch_prefix: str = BRANCH_PREFIX,
        keep_on_success: bool = False,
        max_workspaces: int = 8,
        push_remote: str = "",
        repo_dir: str = "",
        neighbours: Sequence[Neighbour] = (),
        superproject: Path | str | None = None,
        superproject_ref: str = "HEAD",
        superproject_remote: str = "",
    ) -> None:
        self.origin = Path(origin).expanduser().resolve()
        self.root = Path(root).expanduser().resolve()
        self.base_ref = base_ref
        self.branch_prefix = branch_prefix
        self.keep_on_success = keep_on_success
        self.max_workspaces = max_workspaces
        # Empty means "keep the work local". Publishing is opt-in because it
        # needs a credential on the runner and because not every deployment
        # wants a branch per attempt in its forge.
        self.push_remote = push_remote
        # The working copy's own directory name inside the container. It
        # matters when a neighbour's path dependency is written relative to it,
        # so it defaults to the repository name rather than something generic.
        self.repo_dir = repo_dir or self.origin.name.removesuffix(".git")
        if not _NEIGHBOUR_RE.match(self.repo_dir):
            raise WorkspaceError(f"unsafe repository directory name: {self.repo_dir!r}")
        self.neighbours = tuple(neighbours)
        self.superproject = Path(superproject).expanduser().resolve() if superproject else None
        self.superproject_ref = superproject_ref
        self.superproject_remote = superproject_remote
        if self.neighbours and self.superproject is None:
            # Placing neighbours at "whatever their main is" is the failure this
            # exists to prevent, so it is refused at construction rather than
            # improvised per task.
            raise WorkspaceError("neighbours require a superproject that pins their revisions")
        self.root.mkdir(parents=True, exist_ok=True)

    # -- lifecycle -------------------------------------------------------------

    def branch_for(self, key: str) -> str:
        return f"{self.branch_prefix}{key}"

    def container_for(self, key: str) -> Path:
        return self.root / key

    def path_for(self, key: str) -> Path:
        return self.container_for(key) / self.repo_dir

    def push_remote_url(self) -> str | None:
        """Where published branches go, as a URL a reviewer or a skill can use.

        ``None`` without a push remote, or when the remote cannot be shared
        (a local path; see :func:`public_remote_url`).
        """
        if not self.push_remote:
            return None
        url = _git(self.origin, "remote", "get-url", self.push_remote, check=False)
        return public_remote_url(url) if url else None

    @property
    def base_branch(self) -> str:
        """Name of the branch copies are cut from, resolved once per call.

        ``base_ref`` may be a literal ref name, or the default ``HEAD`` — which
        is not a branch and cannot be a fetch destination. In the latter case
        the repository's own HEAD says which branch it means.
        """
        if self.base_ref != "HEAD":
            return self.base_ref
        head = _git(self.origin, "symbolic-ref", "--quiet", "HEAD", check=False)
        return head.removeprefix("refs/heads/") or "main"

    def acquire(self, key: str, base_branch: str | None = None) -> Workspace:
        """Take exclusive ownership of this task's copy, creating it if needed.

        Reuse is the normal path after a restart: the same key gives back the
        same copy with its uncommitted changes intact.

        ``base_branch`` is the task's own base (``customFields.baseBranch``,
        a feature branch of TAI-ADR-0047). A copy is then cut from that branch
        as the forge has it, and a branch the forge does not have fails the
        acquisition — falling back to the default base would hand the agent
        the wrong code without anyone noticing. ``None`` keeps the pool's
        default base.
        """
        if not _KEY_RE.match(key):
            raise WorkspaceError(f"unsafe workspace key: {key!r}")
        if base_branch is not None:
            _check_branch_name(base_branch)
        branch = self.branch_for(key)
        container = self.container_for(key)
        path = self.path_for(key)
        lock_fd = self._lock(key)
        try:
            # Drop records of copies deleted behind git's back, otherwise
            # ``worktree add`` refuses the path as already registered.
            _git(self.origin, "worktree", "prune")
            self._adopt_flat_copy(container, path)
            base_ref = self._refresh_base(base_branch)
            wanted = base_branch or self.base_branch
            self._settle_base(path, branch, wanted)
            reused = path.exists()
            if reused:
                self._verify(path, branch)
            else:
                self._create(path, branch, base_ref, wanted)
            neighbours = self._place_neighbours(container)
            base = _git(path, "rev-parse", "HEAD")
        except Exception:
            os.close(lock_fd)
            raise
        return Workspace(
            key=key,
            branch=branch,
            path=path,
            base_commit=base,
            reused=reused,
            neighbours=neighbours,
            base_branch=wanted,
            _lock_fd=lock_fd,
        )

    def release(self, workspace: Workspace, outcome: Outcome) -> None:
        """Give the copy back. Cleanup never destroys unmerged work.

        A failed or suspended run keeps its copy verbatim — that is the state a
        later attempt resumes from. A successful one may drop the working copy,
        but the branch always stays: the commit is the evidence.
        """
        try:
            if outcome == "succeeded" and not self.keep_on_success:
                self._remove(workspace)
            self._enforce_disk_budget(keep=workspace.key)
        finally:
            if workspace._lock_fd is not None:
                os.close(workspace._lock_fd)
                workspace._lock_fd = None

    # -- internals -------------------------------------------------------------

    def _lock(self, key: str) -> int:
        import fcntl

        locks = self.root / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        fd = os.open(locks / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise WorkspaceBusyError(f"workspace {key} is held by another process") from exc
        return fd

    def _refresh_base(self, base_branch: str | None = None) -> str:
        """Fetch the base branch and return the ref a copy should be cut from.

        Without this the runner branches from whatever it had when it was last
        updated by hand, and the agent fixes code that no longer exists — the
        work then arrives as a branch whose diff looks like it reverts whatever
        landed meanwhile. Observed for real: a task branched one commit behind,
        and its diff read as an undo of the change in between.

        The fetch lands in the remote-tracking ref rather than in the local
        branch, and that ref is what we branch from. Updating the local branch
        directly is what git refuses when a working copy has it checked out —
        true for a plain repository, and true on a runner the moment someone
        opens a copy of the base for themselves.

        Best-effort on purpose. A runner may have no upstream at all, and an
        unreachable forge must not stop work that can proceed from what is
        already on disk — so the fallback is the configured ``base_ref``.

        A task's own ``base_branch`` is stricter, see :meth:`_refresh_task_base`.
        """
        if base_branch is not None:
            return self._refresh_task_base(base_branch)
        if not self.push_remote:
            return self.base_ref
        branch = self.base_branch
        result = subprocess.run(
            ["git", "fetch", "--quiet", self.push_remote, branch],
            cwd=self.origin,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "could not refresh %s from %s: %s",
                branch,
                self.push_remote,
                redact_local_paths(result.stderr.strip())[:200],
            )
            return self.base_ref
        return "FETCH_HEAD"

    def _refresh_task_base(self, base_branch: str) -> str:
        """Fetch a task's own base branch and return the ref to cut from.

        Unlike the default base, a missing branch is an error: the fallback
        would be the default branch, and work cut from it arrives at review
        as a diff against the wrong line. Only an unreachable forge (not a
        branch the forge says it lacks) falls back to a copy the mirror
        already holds, the same bargain as the default base.

        The fetch lands in a ref of its own rather than in FETCH_HEAD: tasks
        of different features acquire copies concurrently, and a shared
        FETCH_HEAD would let one task be cut from another's branch.
        """
        local = f"refs/heads/{base_branch}"
        if not self.push_remote:
            if not self._has_ref(local):
                raise WorkspaceError(f"base branch {base_branch} does not exist in the repository")
            return local
        tracking = f"refs/remotes/{self.push_remote}/{base_branch}"
        result = subprocess.run(
            ["git", "fetch", "--quiet", self.push_remote, f"+{local}:{tracking}"],
            cwd=self.origin,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return tracking
        stderr = result.stderr.strip()
        if "couldn't find remote ref" in stderr:
            raise WorkspaceError(f"base branch {base_branch} does not exist in {self.push_remote}")
        logger.warning(
            "could not refresh %s from %s: %s",
            base_branch,
            self.push_remote,
            redact_local_paths(stderr)[:200],
        )
        for ref in (tracking, local):
            if self._has_ref(ref):
                return ref
        raise WorkspaceError(
            f"base branch {base_branch} could not be fetched from {self.push_remote} "
            "and is not in the mirror"
        )

    def _has_ref(self, ref: str) -> bool:
        return bool(_git(self.origin, "rev-parse", "--verify", "--quiet", ref, check=False))

    def _recorded_base(self, branch: str) -> tuple[str, str]:
        """(base branch, base commit) the task branch was cut from, as recorded.

        Branches made before the record existed were cut from the default base,
        so that is what an absent record means; their base commit is unknown.
        """
        recorded = _git(
            self.origin, "config", "--get", f"branch.{branch}.{_BASE_BRANCH_KEY}", check=False
        )
        commit = _git(
            self.origin, "config", "--get", f"branch.{branch}.{_BASE_COMMIT_KEY}", check=False
        )
        return recorded or self.base_branch, commit

    def _settle_base(self, path: Path, branch: str, wanted: str) -> None:
        """Make sure an existing task branch was cut from the base asked for now.

        A task whose ``baseBranch`` changed since its copy was made (set after
        a first attempt, or moved to another feature) must not silently resume
        on the old line: its work would be reviewed and merged against the
        wrong branch. A branch that holds no work yet is recreated from the new
        base; one that holds work — uncommitted changes or commits of its own —
        is refused with the reason, because moving that work is a person's
        decision and this module never destroys it.
        """
        if not self._has_ref(f"refs/heads/{branch}"):
            return
        recorded, base_commit = self._recorded_base(branch)
        if recorded == wanted:
            return
        if path.exists() and _git(path, "status", "--porcelain"):
            raise WorkspaceError(
                f"{branch} was cut from {recorded}, the task now asks for {wanted}, "
                "and the copy holds uncommitted changes; move them by hand"
            )
        own = self._own_commits(branch, base_commit)
        if own:
            raise WorkspaceError(
                f"{branch} was cut from {recorded}, the task now asks for {wanted}, "
                f"and the branch holds {own} commit(s) of its own; move them by hand"
            )
        logger.info("recreating %s: cut from %s, task now asks for %s", branch, recorded, wanted)
        if path.exists():
            # The copy is clean (checked above): whatever is left is ignored
            # files, caches and a .venv, which --force may drop.
            _git(self.origin, "worktree", "remove", "--force", str(path))
        _git(self.origin, "branch", "-D", branch)

    def _own_commits(self, branch: str, base_commit: str) -> int:
        """Commits on the task branch past the point it was cut from."""
        if base_commit:
            spec = [f"{base_commit}..refs/heads/{branch}"]
        else:
            # No record of the cut point: count what no other ref reaches.
            # Refs only: ``--all`` would count the copy's own HEAD, which
            # reaches every commit of the branch.
            spec = [f"refs/heads/{branch}", "--not", f"--exclude={branch}", "--branches"]
            spec += ["--tags", "--remotes"]
        return int(_git(self.origin, "rev-list", "--count", *spec) or "0")

    def _adopt_flat_copy(self, container: Path, path: Path) -> None:
        """Move a copy made before containers existed into the new layout.

        Runners carry unfinished work: copies of failed and suspended runs are
        exactly the state a later attempt resumes from. Recreating them under
        the new layout would either fork a second copy of the branch or make
        git refuse the branch as already checked out, so the existing copy is
        moved rather than abandoned — with its uncommitted changes.
        """
        if path.exists() or not (container / ".git").exists():
            return
        staging = self.root / f".adopt-{container.name}"
        # Two moves because git cannot move a worktree into a subdirectory of
        # itself, and the container is exactly that.
        _git(self.origin, "worktree", "move", str(container), str(staging))
        container.mkdir(parents=True, exist_ok=True)
        _git(self.origin, "worktree", "move", str(staging), str(path))
        logger.info("adopted %s into the container layout", container.name)

    # -- neighbours ------------------------------------------------------------

    def _place_neighbours(self, container: Path) -> dict[str, str]:
        """Lay out the repositories this copy must build against.

        Returns the revision each one was placed at, which travels into the
        checkpoint: a green test run is only meaningful together with the
        revisions it ran against.
        """
        if not self.neighbours:
            return {}
        ref = self._refresh_superproject()
        placed: dict[str, str] = {}
        for neighbour in self.neighbours:
            revision = self._pinned_revision(neighbour, ref)
            self._place_neighbour(container / neighbour.path, neighbour, revision)
            placed[neighbour.path] = revision
        return placed

    def _pinned_revision(self, neighbour: Neighbour, ref: str) -> str:
        """Revision the superproject pins for this neighbour, from its tree."""
        assert self.superproject is not None  # guarded in __init__
        entry = _git(self.superproject, "ls-tree", ref, neighbour.path)
        fields = entry.split()
        if len(fields) < 3 or fields[0] != _GITLINK_MODE:
            raise WorkspaceError(
                f"{neighbour.path} is not a submodule of the superproject at {ref}"
            )
        return fields[2]

    def _ensure_neighbour_revision(self, neighbour: Neighbour, revision: str) -> None:
        """Fetch the neighbour mirror when it does not yet hold the pinned commit.

        The superproject moves its pin whenever the neighbour's own main
        moves; a bare mirror on the runner only knows what it was last told.
        Observed for real: the pin advanced, the mirror did not, and every
        task died in `git worktree add` with "invalid reference" until someone
        fetched by hand. Best-effort: an unreachable forge leaves the clear
        error that follows, not a silent stale checkout.
        """
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=neighbour.origin,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return
        result = subprocess.run(
            ["git", "fetch", "--quiet", "origin", "+refs/heads/*:refs/heads/*"],
            cwd=neighbour.origin,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "could not fetch neighbour %s for %s: %s",
                neighbour.path,
                revision[:12],
                redact_local_paths(result.stderr.strip())[:200],
            )
        else:
            logger.info("fetched neighbour %s to reach %s", neighbour.path, revision[:12])

    def _place_neighbour(self, dest: Path, neighbour: Neighbour, revision: str) -> None:
        self._ensure_neighbour_revision(neighbour, revision)
        if not dest.exists():
            _git(neighbour.origin, "worktree", "prune")
            _git(neighbour.origin, "worktree", "add", "--detach", str(dest), revision)
            return
        if _git(dest, "rev-parse", "HEAD", check=False) == revision:
            return
        if _git(dest, "status", "--porcelain", check=False):
            # Somebody — the agent, or a person debugging — has work in there.
            # Moving the checkout under it would destroy that work silently,
            # and this module never does that.
            logger.info("keeping local changes in %s; left off the pinned revision", neighbour.path)
            return
        _git(dest, "checkout", "--quiet", "--detach", revision)

    def _refresh_superproject(self) -> str:
        """Update the pin source, best-effort, and return the ref to read.

        Same bargain as ``_refresh_base``: an unreachable forge must not stop
        work that can proceed from what is already on disk. Without a remote
        the mirror is whatever the deployment last put there — deliberate, so
        that a pool never guesses which remote a superproject belongs to.
        """
        assert self.superproject is not None  # guarded in __init__
        if not self.superproject_remote:
            return self.superproject_ref
        ref = self.superproject_ref if self.superproject_ref != "HEAD" else "HEAD"
        result = subprocess.run(
            ["git", "fetch", "--quiet", self.superproject_remote, ref],
            cwd=self.superproject,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "could not refresh the superproject from %s: %s",
                self.superproject_remote,
                redact_local_paths(result.stderr.strip())[:200],
            )
            return self.superproject_ref
        return "FETCH_HEAD"

    def _create(self, path: Path, branch: str, base_ref: str, base_branch: str) -> None:
        exists = self._has_ref(f"refs/heads/{branch}") or self._adopt_published(
            branch, base_ref, base_branch
        )
        if exists:
            # The branch outlived its working copy (cleanup after success, or a
            # pruned copy): continue it instead of forking a second line. Note
            # it is NOT rebased onto the refreshed base — a second attempt must
            # resume the work, not silently move it.
            _git(self.origin, "worktree", "add", str(path), branch)
        else:
            _git(self.origin, "worktree", "add", str(path), "-b", branch, base_ref)
            # What the branch was cut from, so a later attempt can tell whether
            # the task still asks for the same base (``_settle_base``).
            config = f"branch.{branch}"
            _git(self.origin, "config", f"{config}.{_BASE_BRANCH_KEY}", base_branch)
            head = _git(path, "rev-parse", "HEAD")
            _git(self.origin, "config", f"{config}.{_BASE_COMMIT_KEY}", head)

    def _adopt_published(self, branch: str, base_ref: str, base_branch: str) -> bool:
        """Take the task branch from the forge when this mirror does not have it.

        A task returned by its verification (a rejected review, a merge that
        failed) is taken again, possibly by another runner or after the mirror
        was rebuilt. Its published branch is where the work and the review
        history are: cutting a fresh one from the base would fork a second
        line that the never-forced push then cannot publish. Best-effort like
        the base refresh: a branch the forge lacks, or an unreachable forge,
        leaves the copy to be cut from the base as before.
        """
        if not self.push_remote:
            return False
        local = f"refs/heads/{branch}"
        # FETCH_HEAD may be the base this copy is about to be cut from.
        result = subprocess.run(
            [
                "git",
                "fetch",
                "--quiet",
                "--no-write-fetch-head",
                self.push_remote,
                f"{local}:{local}",
            ],
            cwd=self.origin,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "couldn't find remote ref" not in stderr:
                logger.warning(
                    "could not look for %s in %s: %s",
                    branch,
                    self.push_remote,
                    redact_local_paths(stderr)[:200],
                )
            return False
        # Recorded as if cut here, from the point it shares with the base, so
        # a later change of base still sees the commits of its own.
        config = f"branch.{branch}"
        _git(self.origin, "config", f"{config}.{_BASE_BRANCH_KEY}", base_branch)
        fork = _git(self.origin, "merge-base", local, base_ref, check=False)
        if fork:
            _git(self.origin, "config", f"{config}.{_BASE_COMMIT_KEY}", fork)
        logger.info("continuing %s as published in %s", branch, self.push_remote)
        return True

    def _verify(self, path: Path, branch: str) -> None:
        if not (path / ".git").exists():
            raise WorkspaceError(f"{path.name} exists but is not a git worktree")
        current = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            # Silently switching would mix two tasks in one copy — the exact
            # failure this module exists to prevent.
            raise WorkspaceError(f"{path.name} is on branch {current}, expected {branch}")

    def _remove(self, workspace: Workspace) -> None:
        if workspace.is_dirty:
            logger.info("keeping %s: uncommitted changes", workspace.key)
            return
        # is_dirty above already proved the copy holds no work: `status
        # --porcelain` is empty, so every remaining file is ignored (a .venv,
        # caches). Without --force git still refuses to drop a copy that has
        # ignored files, and on a runner that leaves ~130 MB per finished task
        # behind until the disk budget prunes it. Forcing here destroys nothing
        # that could be evidence.
        _git(self.origin, "worktree", "remove", "--force", str(workspace.path), check=False)
        if workspace.path.exists():
            logger.info("keeping %s: git declined to remove the worktree", workspace.key)
            return
        self._drop_neighbours(workspace.container)

    def _drop_neighbours(self, container: Path) -> None:
        """Take the neighbours down with the copy they were placed for.

        They are cheap to recreate and hold no work of their own — the agent's
        changes live on the task branch of the working copy — but they are
        worktrees, so leaving them behind leaves both disk and stale records in
        their own repositories.
        """
        for neighbour in self.neighbours:
            dest = container / neighbour.path
            if dest.exists():
                _git(neighbour.origin, "worktree", "remove", str(dest), check=False)
        with contextlib.suppress(OSError):
            container.rmdir()  # only when nothing else is left in it

    def _enforce_disk_budget(self, *, keep: str) -> None:
        """Bound how much disk idle copies may hold. Branches are never touched."""
        candidates = sorted(self._idle_workspaces(keep=keep), key=lambda item: item[1])
        excess = len(candidates) - self.max_workspaces
        for container, _ in candidates[: max(0, excess)]:
            _git(self.origin, "worktree", "remove", str(container / self.repo_dir), check=False)
            if (container / self.repo_dir).exists():
                continue
            self._drop_neighbours(container)
            logger.info("pruned idle workspace %s", container.name)

    def _idle_workspaces(self, *, keep: str) -> Iterable[tuple[Path, float]]:
        import fcntl

        for path in self.root.iterdir():
            if not path.is_dir() or path.name in {".locks", keep}:
                continue
            copy = path / self.repo_dir
            if not copy.is_dir():
                continue  # not a container of ours: leave it alone
            if _git(copy, "status", "--porcelain", check=False):
                continue  # holds uncommitted work
            lock_path = self.root / ".locks" / f"{path.name}.lock"
            if lock_path.exists():
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    continue  # someone is working in it right now
                finally:
                    os.close(fd)
            yield path, path.stat().st_mtime


def remove_pool(root: Path | str) -> None:  # pragma: no cover - operational helper
    """Delete a pool root outright. For tests and manual cleanup only."""
    shutil.rmtree(Path(root), ignore_errors=True)
