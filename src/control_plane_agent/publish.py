"""Publishing task branches: the target check, and what is still to publish (universal-runner U007).

Every push of the daemon goes through :func:`publish_branch`. Before git is
asked to push, the target is checked twice (TAI-ADR-0063, FR-012):

* every address git would push to must be the one the catalog names for the
  repository — a remote that points elsewhere, or also elsewhere, is refused
  here, without asking anyone; and only a task branch is pushed, exactly the
  commit that was checked;
* then the publish hook, when the host configures one, decides: it gets the
  catalog address, the branch and the commit, and allows or refuses. The hook
  is what can ask the forge itself (a private repository, the full name the
  catalog expects — an address the forge redirects is not the one the catalog
  named). It is a command of the host (:data:`ENV_PUBLISH_HOOK`), not of the
  agent: the implementation lives with the forge's credentials, the daemon
  only calls it.

A refused target is :data:`PUBLISH_TARGET_REJECTED`: nothing is pushed, and it
is not retried — it is a person's business. A push that failed (the network,
the forge) is, and so is a hook that could not check for now
(:data:`HOOK_EXIT_TEMPFAIL`: the forge answered 5xx, the network failed): the
branch goes to the replica's list of unpublished work
(:class:`UnpublishedLedger`, a file in the root of its working copies, which
outlives the process) and is pushed again at the start of a cycle — the next
one, then after a pause that grows with every failure (:func:`retry_delay`).
Not for ever: after :data:`RETRY_MAX_FAILURES` failures in a row, or
:data:`RETRY_MAX_AGE_SECONDS` after the first, the entry is given up
(:meth:`Unpublished.exhausted`) — a forge that keeps refusing (a revoked
token, a protected branch, a pre-receive hook) is a person's business too.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from control_plane_agent.workspace import ExecutionWorkspacePool, redact_local_paths

logger = logging.getLogger("control_plane_agent.publish")

#: The failure reason of a run whose branch the target check refused.
PUBLISH_TARGET_REJECTED = "publish_target_rejected"
#: The host's publish hook: a command line, split as a shell would, run without one.
ENV_PUBLISH_HOOK = "CONTROL_PLANE_AGENT_PUBLISH_HOOK"
#: How long the hook may take before its silence counts as a refusal.
HOOK_TIMEOUT_SECONDS = 60.0
#: The hook's exit code for "cannot check now" (``EX_TEMPFAIL``): retried, not refused.
HOOK_EXIT_TEMPFAIL = 75
#: The unpublished list, in the root of the replica's working copies.
LEDGER_NAME = ".unpublished.json"
#: The pause after the second failed push of a branch; it doubles with each next one.
RETRY_BASE_SECONDS = 30.0
#: The longest pause between two pushes of one branch.
RETRY_MAX_SECONDS = 1800.0
#: Failed pushes of one branch in a row after which it is given up.
RETRY_MAX_FAILURES = 12
#: How long after its first failed push a branch is given up, whatever the count.
RETRY_MAX_AGE_SECONDS = 6 * 3600.0
_MAX_REASON_CHARS = 300
# Credentials in an address git may print: ``scheme://user:token@host``.
_USERINFO_RE = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")

Published = Literal["published", "failed", "rejected", "local"]


@dataclass(frozen=True)
class PublishTarget:
    """What is about to be published, as the hook receives it."""

    # The repository's address as the catalog names it (the one-repository
    # form: ``workingCopy.repository``), never one taken from a task.
    url: str
    branch: str
    commit: str
    # Key of the repository in the catalog; empty for the one-repository form.
    repository_key: str = ""

    def as_json(self) -> dict[str, str]:
        return {
            "url": self.url,
            "branch": self.branch,
            "commit": self.commit,
            "repositoryKey": self.repository_key,
        }


class PublishHookUnavailable(Exception):
    """The hook could not check the target for now; ``reason`` is safe to record.

    Not a refusal: the push counts as failed and is retried like one.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PublishHook(Protocol):
    """Decides whether a branch may go to a target: None allows, a string refuses.

    Raising :class:`PublishHookUnavailable` says neither: ask again later.
    """

    def check(self, target: PublishTarget) -> str | None: ...


class CommandPublishHook:
    """The hook as a command of the host (:data:`ENV_PUBLISH_HOOK`).

    The command gets the target as one JSON object on stdin — ``url``,
    ``branch``, ``commit``, ``repositoryKey`` — and the daemon's environment.
    Exit code 0 allows the push; :data:`HOOK_EXIT_TEMPFAIL` (75) says the
    check could not be made now (:class:`PublishHookUnavailable`: the push is
    retried); any other refuses it. The first line of stdout is the reason.
    A command that cannot be started, or does not
    answer within :data:`HOOK_TIMEOUT_SECONDS`, refuses too: a check that did
    not run is not a check that passed.
    """

    def __init__(self, argv: list[str], *, timeout: float = HOOK_TIMEOUT_SECONDS) -> None:
        if not argv:
            raise ValueError(f"{ENV_PUBLISH_HOOK} names no command")
        self.argv = list(argv)
        self.timeout = timeout

    def check(self, target: PublishTarget) -> str | None:
        try:
            result = subprocess.run(
                self.argv,
                input=json.dumps(target.as_json()),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"the publish hook did not answer in {self.timeout:g}s"
        except OSError as exc:
            return f"the publish hook could not be started: {type(exc).__name__}"
        if result.returncode == 0:
            return None
        if result.stderr.strip():
            logger.info("publish hook: %s", redact_local_paths(result.stderr.strip())[:500])
        first = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
        if result.returncode == HOOK_EXIT_TEMPFAIL:
            raise PublishHookUnavailable(
                _reason(first)
                or f"the publish hook could not check now (exit {HOOK_EXIT_TEMPFAIL})"
            )
        return _reason(first) or f"the publish hook refused (exit {result.returncode})"


def hook_from_environment(environ: Mapping[str, str] | None = None) -> PublishHook | None:
    """The host's hook, or None when :data:`ENV_PUBLISH_HOOK` is unset or blank."""
    values = os.environ if environ is None else environ
    raw = values.get(ENV_PUBLISH_HOOK, "").strip()
    if not raw:
        return None
    return CommandPublishHook(shlex.split(raw))


def _reason(text: str) -> str:
    """A reason as it may travel: one line, no credentials, no local path, bounded."""
    return redact_local_paths(_USERINFO_RE.sub(r"\1", " ".join(text.split())))[:_MAX_REASON_CHARS]


class PublishRejected(Exception):
    """The target check refused the branch; ``reason`` is safe to record."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{PUBLISH_TARGET_REJECTED}: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class PublishResult:
    outcome: Published
    # Why the target was refused (``rejected``) or the push failed (``failed``),
    # as it may be recorded: one line, no credentials, no local path.
    reason: str = ""

    @property
    def published(self) -> bool:
        return self.outcome == "published"


def publish_branch(
    pool: ExecutionWorkspacePool, branch: str, hook: PublishHook | None
) -> PublishResult:
    """Check the target of ``branch`` and push it; never raises, never forces.

    ``local`` — the pool publishes nowhere (``publish: false`` or no remote);
    ``rejected`` — the target check refused, nothing was pushed; ``failed`` —
    git could not push, or the hook could not check now and nothing was
    pushed; ``published`` — the branch is in the forge.
    """
    if not pool.push_remote:
        return PublishResult("local")
    commit = pool.branch_head(branch) if pool.is_task_branch(branch) else None
    if commit is None:
        return PublishResult("failed", "the branch is not a task branch of this mirror")
    actual = pool.push_urls()
    expected = _without_credentials(pool.publish_url or (actual[0] if actual else ""))
    if not actual or not all(same_repository(url, expected) for url in actual):
        # A remote git would push to is not the catalog's: a mirror whose
        # push URL was changed or added to on the host. git pushes to every
        # push URL, so each is checked. The catalog is the only source of
        # addresses (FR-002): refused before any hook is asked.
        return _rejected(branch, "the push remote is not the repository the catalog names")
    if hook is not None:
        target = PublishTarget(
            url=expected, branch=branch, commit=commit, repository_key=pool.repository_key
        )
        try:
            refusal = hook.check(target)
        except PublishHookUnavailable as exc:
            # Not the target's fault: kept to retry, under the retry limits.
            reason = _reason(exc.reason) or "the publish hook could not check now"
            logger.warning("%s: the publish hook could not check now: %s", branch, reason)
            return PublishResult("failed", reason)
        if refusal is not None:
            return _rejected(branch, _reason(refusal) or "the publish hook refused")
    # Exactly the commit the hook was asked about, whatever the branch holds
    # now, and to exactly the addresses checked above: pushing by the remote's
    # name would read them again from a config the executor's copy shares.
    error = pool.push_branch(branch, commit, urls=actual)
    if error is not None:
        return PublishResult("failed", _reason(error) or "git could not push")
    return PublishResult("published")


def _rejected(branch: str, reason: str) -> PublishResult:
    logger.warning("%s: publish_target_rejected: %s", branch, reason)
    return PublishResult("rejected", reason)


def same_repository(left: str, right: str) -> bool:
    """One repository under two spellings: case, a trailing ``/`` or ``.git``.

    Credentials in an https address do not make it another repository.
    """

    def norm(url: str) -> str:
        return _without_credentials(url).lower().rstrip("/").removesuffix(".git")

    return bool(left.strip()) and norm(left) == norm(right)


def _without_credentials(url: str) -> str:
    """``url`` without ``user:token@``: the hook needs the address, not the secret."""
    url = url.strip()
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    return f"{scheme}://{authority.rpartition('@')[2]}{slash}{path}"


# -- the unpublished list ------------------------------------------------------


def retry_delay(failures: int) -> float:
    """The pause before the next push of a branch that failed ``failures`` times.

    The first failure is retried in the next cycle; after that the pause
    doubles from :data:`RETRY_BASE_SECONDS` up to :data:`RETRY_MAX_SECONDS`,
    so a forge that is down, or a push it keeps refusing, is not asked — nor
    is the publish hook — every few seconds for ever.
    """
    if failures <= 1:
        return 0.0
    return min(RETRY_BASE_SECONDS * float(2 ** min(failures - 2, 32)), RETRY_MAX_SECONDS)


@dataclass(frozen=True)
class Unpublished:
    """A branch whose push failed, to be pushed again."""

    task: str
    branch: str
    commit: str
    repository_key: str = ""
    # Pushes of the branch that failed in a row, when the next one is due and
    # when the first one failed (seconds since the epoch: the list outlives
    # the process).
    failures: int = 0
    retry_at: float = 0.0
    first_failed_at: float = 0.0
    # Why the last push failed, as it may be recorded.
    reason: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "branch": self.branch,
            "commit": self.commit,
            "repositoryKey": self.repository_key,
            "failures": self.failures,
            "retryAt": self.retry_at,
            "firstFailedAt": self.first_failed_at,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, value: Any) -> Unpublished | None:
        if not isinstance(value, Mapping):
            return None
        task, branch, commit = value.get("task"), value.get("branch"), value.get("commit")
        key = value.get("repositoryKey", "")
        if not (isinstance(task, str) and isinstance(branch, str) and isinstance(commit, str)):
            return None
        if not (task and branch and commit) or not isinstance(key, str):
            return None
        # A pause that cannot be read is no pause: the entry is retried now.
        # A start that cannot be read is unknown (0): only the count limits it.
        failures = value.get("failures")
        if not (isinstance(failures, int) and not isinstance(failures, bool) and failures >= 0):
            failures = 0
        reason = value.get("reason", "")
        return cls(
            task,
            branch,
            commit,
            key,
            failures=failures,
            retry_at=_seconds(value.get("retryAt")),
            first_failed_at=_seconds(value.get("firstFailedAt")),
            reason=_reason(reason) if isinstance(reason, str) else "",
        )

    def due(self, now: float) -> bool:
        """Whether the next push is due; a pause beyond the longest one is not believed."""
        return self.retry_at <= now or self.retry_at - now > RETRY_MAX_SECONDS

    def exhausted(self, now: float) -> bool:
        """Whether the branch is given up: too many failures, or failing for too long."""
        if self.failures >= RETRY_MAX_FAILURES:
            return True
        return 0 < self.first_failed_at <= now - RETRY_MAX_AGE_SECONDS


def _seconds(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return 0.0


class UnpublishedLedger:
    """Branches this replica could not publish, kept on its volume.

    One entry per (repository, branch): a later failure of the same branch
    replaces the commit, a successful push removes the entry. Reads and
    writes hold a file lock, and a write replaces the file whole, so two
    processes on one root, or a crash mid-write, leave a list and not a
    fragment of one. A file that cannot be read is logged and treated as
    empty by the retries: the branches are still in the mirror, and the next
    failure writes a fresh list. Whoever must tell "nothing waits" from "what
    waits is unknown" (the disk budget) reads :meth:`read` instead.
    """

    def __init__(self, root: Path) -> None:
        self.path = root / LEDGER_NAME
        self._lock_path = root / f"{LEDGER_NAME}.lock"

    def entries(self) -> list[Unpublished]:
        return self.read() or []

    def read(self) -> list[Unpublished] | None:
        """The entries; None when the file is there but cannot be read or parsed."""
        with self._locked():
            return self._read()

    def add(self, entry: Unpublished, *, now: float | None = None) -> Unpublished:
        """Record a failed push of ``entry.branch``; the pause grows with each one.

        The entry as recorded is returned. One that is :meth:`exhausted
        <Unpublished.exhausted>` by this failure is not kept: it is returned
        so that the caller gives it up where a person sees it.
        """
        at = time.time() if now is None else now
        with self._locked():
            entries = self._read() or []
            previous = next((e for e in entries if _same_entry(e, entry)), None)
            failures = (previous.failures if previous is not None else 0) + 1
            first = previous.first_failed_at if previous is not None else 0.0
            if not 0 < first <= at:
                # Unknown, or in the future (a clock set back): counted from now.
                first = at
            kept = [e for e in entries if not _same_entry(e, entry)]
            recorded = replace(
                entry,
                failures=failures,
                retry_at=at + retry_delay(failures),
                first_failed_at=first,
                reason=_reason(entry.reason),
            )
            self._write(kept if recorded.exhausted(at) else [*kept, recorded])
            return recorded

    def discard(self, repository_key: str, branch: str) -> None:
        with self._locked():
            entries = self._read() or []
            kept = [e for e in entries if (e.repository_key, e.branch) != (repository_key, branch)]
            if len(kept) != len(entries):
                self._write(kept)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self) -> list[Unpublished] | None:
        """The entries; [] without a file, None for a file that cannot be read."""
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("the unpublished list cannot be read (%s)", exc)
            return None
        items = raw.get("items") if isinstance(raw, Mapping) else None
        if not isinstance(items, list):
            logger.warning("the unpublished list has no items")
            return None
        parsed = [Unpublished.from_json(item) for item in items]
        return [entry for entry in parsed if entry is not None]

    def _write(self, entries: list[Unpublished]) -> None:
        staging = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        staging.write_text(json.dumps({"items": [e.as_json() for e in entries]}, indent=1))
        os.replace(staging, self.path)


def _same_entry(left: Unpublished, right: Unpublished) -> bool:
    return (left.repository_key, left.branch) == (right.repository_key, right.branch)


__all__ = [
    "ENV_PUBLISH_HOOK",
    "HOOK_EXIT_TEMPFAIL",
    "HOOK_TIMEOUT_SECONDS",
    "PUBLISH_TARGET_REJECTED",
    "RETRY_BASE_SECONDS",
    "RETRY_MAX_AGE_SECONDS",
    "RETRY_MAX_FAILURES",
    "RETRY_MAX_SECONDS",
    "CommandPublishHook",
    "PublishHook",
    "PublishHookUnavailable",
    "PublishRejected",
    "PublishResult",
    "PublishTarget",
    "Unpublished",
    "UnpublishedLedger",
    "hook_from_environment",
    "publish_branch",
    "retry_delay",
    "same_repository",
]
