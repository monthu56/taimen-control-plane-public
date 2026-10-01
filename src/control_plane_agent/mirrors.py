"""Bare mirrors on the runner host (universal-runner U005, U006).

Copies of a task's repository are cut from a bare mirror of it, and
neighbours from mirrors of their own: ``<mirrors>/<name>.git`` holds the task
branches of a pool, ``<mirrors>/neighbours/<key>.git`` only what the forge
has, fetched into ``refs/remotes/origin/*``. A neighbour is often a repository
this host also runs tasks of (a memory-service task builds against
control-plane), and a fetch of ``refs/heads/*`` into the pool's mirror would
move that pool's task branches under its copies (FR-006).

Replicas on one host share the mirrors directory, so a mirror is made and
fetched under a file lock next to it. The lock waits a bounded time: a clone
hung in another replica is an error with a reason, not a pool that waits
forever. A clone goes to a staging directory first and is moved in when it is
complete; staging left by a process that died is swept at start
(:func:`sweep_stale_clones`).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from control_plane_agent.workspace import (
    BASE_FETCH_TIMEOUT,
    WorkspaceError,
    _run_bounded,
    public_remote_url,
    redact_local_paths,
)

logger = logging.getLogger("control_plane_agent.mirrors")

#: How long a replica waits for another one to finish a clone or a fetch.
MIRROR_LOCK_TIMEOUT = 600.0
_POLL_SECONDS = 0.2
# ``.<name>.git.<pid>.tmp``: a clone in progress, or left by a process that died.
_STAGING_RE = re.compile(r"^\.(?P<name>.+\.git)\.(?P<pid>\d+)\.tmp\Z")
# The key of a neighbour mirror is a catalog key (catalog._KEY_RE).
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")
REMOTE_HEADS = "+refs/heads/*:refs/remotes/origin/*"
# What git says when the forge was reached and refused an object it does not
# serve: ``not our ref`` of upload-pack (protocol v2, and v0 with any-sha
# wants), ``unadvertised object`` of a v0 client. Anything else — a refused
# connection, a hung-up transport, a timeout — says nothing about the commit.
_REFUSED_RE = re.compile(r"not our ref|does not allow request for unadvertised object")


class MirrorError(WorkspaceError):
    """A mirror cannot be made or read."""


class MirrorBusyError(MirrorError):
    """Another process holds the mirror's lock longer than this one waits."""


class RevisionMissing(MirrorError):
    """The forge answered a fetch and has no such commit: fetching again changes nothing."""


def repository_name(url: str) -> str:
    """The mirror's name of an address: the last segment of its path, without ``.git``.

    Empty when the address names no repository. A malformed address
    (``https://[abc/x``, a port out of range) is a :class:`MirrorError`, not
    the ``ValueError`` of ``urlsplit``.
    """
    try:
        path = urlsplit(url).path
    except ValueError as exc:
        raise MirrorError(f"repository {shown(url)} is not a valid address: {exc}") from None
    return path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def shown(url: str) -> str:
    """A URL safe for a log line or an error: no credentials, no local path."""
    return public_remote_url(url) or "(a local repository)"


@contextlib.contextmanager
def mirror_lock(path: Path, *, what: str, timeout: float = MIRROR_LOCK_TIMEOUT) -> Iterator[None]:
    """Hold the file lock ``path``; :class:`MirrorBusyError` after ``timeout`` seconds."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MirrorBusyError(
                        f"{what} is held by another process for more than {timeout:.0f} s; "
                        "a clone or fetch there may hang"
                    ) from None
                time.sleep(_POLL_SECONDS)
        yield
    finally:
        os.close(fd)


def lock_path(mirror: Path) -> Path:
    return mirror.with_name(f".{mirror.name}.lock")


def clone_bare(url: str, path: Path) -> None:
    """Clone bare beside ``path`` and move it in: a half-made mirror is never seen.

    The caller holds the mirror's lock.
    """
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    result = subprocess.run(
        # ``--``: an address is never read as an option of git.
        ["git", "clone", "--bare", "--quiet", "--", url, str(staging)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise MirrorError(f"could not mirror {shown(url)}: exit {result.returncode}")
    staging.rename(path)
    logger.info("mirrored %s", shown(url))


def sweep_stale_clones(directory: Path) -> list[str]:
    """Remove staging of clones whose process is gone; the names removed.

    A staging directory is removed only when nobody holds its mirror's lock —
    a clone runs under it, and the lock is shared by replicas on one host,
    whose pids this process cannot see — and its pid is not alive here.
    """
    removed: list[str] = []
    if not directory.is_dir():
        return removed
    for entry in directory.iterdir():
        match = _STAGING_RE.match(entry.name)
        if match is None or not entry.is_dir():
            continue
        pid = int(match["pid"])
        if pid == os.getpid() or _alive(pid):
            continue
        try:
            with mirror_lock(directory / f".{match['name']}.lock", what="", timeout=0):
                shutil.rmtree(entry, ignore_errors=True)
        except MirrorBusyError:
            continue  # a clone of that repository is running somewhere
        removed.append(entry.name)
        logger.info("removed %s, left by a clone that did not finish", entry.name)
    return removed


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, under another user
    return True


class NeighbourMirrors:
    """Mirrors of neighbours and of the superproject, apart from the pools' mirrors.

    ``<root>/<key>.git`` is a bare repository whose ``origin`` is the
    catalog's URL of ``key``; a fetch writes ``refs/remotes/origin/*`` only.
    Copies placed from it are detached at a pinned revision, so nothing here
    ever has a branch of its own to lose.

    A fetch runs at most ``fetch_timeout`` seconds, killed with its transport
    after that, like the fetch of a pool's base: a hung forge of a neighbour
    or of the superproject fails the fetch instead of hanging the copy, and
    the lock of its mirror, for every replica.
    """

    def __init__(
        self,
        root: Path,
        *,
        lock_timeout: float = MIRROR_LOCK_TIMEOUT,
        fetch_timeout: float = BASE_FETCH_TIMEOUT,
    ) -> None:
        self.root = root
        self.lock_timeout = lock_timeout
        self.fetch_timeout = fetch_timeout

    def path_of(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise MirrorError(f"unsafe neighbour key: {key!r}")
        return self.root / f"{key}.git"

    def ensure(self, key: str, url: str) -> Path:
        """The mirror of ``key``, made when missing, pointed at ``url`` when it moved.

        A neighbour mirror holds nothing of its own — only what a forge had and
        the revisions placed from it — so a new address of the catalog is
        taken as is: ``origin`` is rewritten and the branches of the old
        address are dropped, the pins stay for the copies placed at them.
        """
        path = self.path_of(key)
        if not path.is_dir():
            self.root.mkdir(parents=True, exist_ok=True)
            with mirror_lock(
                lock_path(path), what=f"mirror {path.name}", timeout=self.lock_timeout
            ):
                if not path.is_dir():
                    self._init(path, url)
        current = _output(path, "config", "--get", "remote.origin.url")
        if current is None or _normalised(current) != _normalised(url):
            with mirror_lock(
                lock_path(path), what=f"mirror {path.name}", timeout=self.lock_timeout
            ):
                self._repoint(path, url)
            logger.info(
                "neighbour mirror %s moved from %s to %s",
                path.name,
                shown(current or "(no origin)"),
                shown(url),
            )
        return path

    def _repoint(self, path: Path, url: str) -> None:
        stale = _output(path, "for-each-ref", "--format=delete %(refname)", "refs/remotes/origin")
        steps = [
            # ``config``, not ``remote set-url``: the value is never read as an option.
            (["git", "config", "remote.origin.url", url], None),
            (["git", "update-ref", "--stdin"], f"{stale}\n" if stale else ""),
        ]
        for argv, stdin in steps:
            result = subprocess.run(
                argv, cwd=path, input=stdin, capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                raise MirrorError(
                    f"could not point the mirror {path.name} at {shown(url)}: "
                    f"exit {result.returncode}"
                )

    def fetch(self, key: str, *refspecs: str) -> bool:
        """Fetch the forge's branches (and ``refspecs``); False when it failed.

        Best-effort, like the refresh of a base: an unreachable forge leaves
        what the mirror already holds, and the caller says what is missing.
        """
        try:
            return self._fetch(key, *refspecs).returncode == 0
        except MirrorBusyError as exc:
            logger.warning("could not fetch %s: %s", self.path_of(key).name, exc)
            return False

    def _fetch(self, key: str, *refspecs: str) -> subprocess.CompletedProcess[str]:
        """The fetch of :meth:`fetch`; :class:`MirrorBusyError` when the lock is held too long."""
        path = self.path_of(key)
        with mirror_lock(lock_path(path), what=f"mirror {path.name}", timeout=self.lock_timeout):
            result = _run_bounded(
                ["git", "fetch", "--quiet", "origin", REMOTE_HEADS, *refspecs],
                path,
                self.fetch_timeout,
            )
        if result.returncode != 0:
            logger.warning(
                "could not fetch %s: %s",
                path.name,
                redact_local_paths(result.stderr.strip())[:200],
            )
        return result

    def reach(self, key: str, revision: str) -> None:
        """Make ``revision`` a commit of the mirror of ``key``, fetching when needed.

        A lock another replica holds too long is :class:`MirrorBusyError`:
        the forge may well have the revision, so "not in its forge" would
        send a person looking for the wrong fault.

        A forge that answered the fetch of its branches and then refused the
        commit by its id is :class:`RevisionMissing` — a pointer to a commit
        that was never pushed, which a person fixes. A forge that did not
        answer either fetch (a network failure, a timeout) is a plain
        :class:`MirrorError`: it said nothing about the commit, and a later
        attempt may succeed.
        """
        path = self.path_of(key)
        if has_commit(path, revision):
            return
        answered = self._fetch(key).returncode == 0
        if has_commit(path, revision):
            return
        # A pin no branch of the forge reaches any more (a rewritten branch):
        # forges hand out a commit by its id. Kept under a ref, so the check
        # for commits of a neighbour's own does not count it.
        by_id = self._fetch(key, f"+{revision}:refs/remotes/pins/{revision}")
        if has_commit(path, revision):
            return
        if answered and by_id.returncode != 0 and _REFUSED_RE.search(by_id.stderr):
            raise RevisionMissing(f"neighbour {key}: revision {revision[:12]} is not in its forge")
        raise MirrorError(
            f"neighbour {key}: revision {revision[:12]} is not in its mirror, "
            "and its forge could not be fetched"
        )

    def _init(self, path: Path, url: str) -> None:
        staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "init", "--bare", "--quiet", str(staging)], check=True, capture_output=True
            )
            # ``config``, not ``remote add``: the value is never read as an option.
            subprocess.run(
                ["git", "config", "remote.origin.url", url],
                cwd=staging,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise MirrorError(
                f"could not make the mirror {path.name}: exit {exc.returncode}"
            ) from None
        staging.rename(path)
        logger.info("neighbour mirror %s made for %s", path.name, shown(url))


def has_commit(repository: Path, revision: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def _normalised(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git")


def _output(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


__all__ = [
    "MIRROR_LOCK_TIMEOUT",
    "MirrorBusyError",
    "MirrorError",
    "NeighbourMirrors",
    "RevisionMissing",
    "clone_bare",
    "has_commit",
    "lock_path",
    "mirror_lock",
    "repository_name",
    "shown",
    "sweep_stale_clones",
]
