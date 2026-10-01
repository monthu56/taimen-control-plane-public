"""The executor says "stopped, not done" (declarative-cycle C006).

An executor that could not do the work — no access, a contradictory task, a
decision only a person can make — must not have its run published as a
success: the task would be completed and its acceptance would start on work
that was never done (TASK-000642). The signal is structural, never read out
of the summary's prose:

* a checkpoint of kind :data:`CHECKPOINT_KIND` on the run, ``data.reason``
  saying why. An executor with the Control Plane tools leaves it itself
  (``cp_checkpoint``); the platform contract of the instructions says so;
* an executor without them (Codex) writes the reason into the file named by
  :data:`ENV_BLOCKED_FILE`, and its adapter turns the file into the same
  checkpoint (:func:`record_blocked_file`).

The daemon reads the checkpoint after the executor returns
(:func:`blocked_reason`): the run fails with :data:`FAILURE_REASON`, the task
goes to the first ``blocked`` status of its lifecycle with the reason in a
comment, the claim is released, and no runner takes it again until a person
returns it to work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from control_plane_agent.workspace import redact_local_paths
from control_plane_client import ControlPlaneClient, ControlPlaneError

logger = logging.getLogger("control_plane_agent.blocked")

CHECKPOINT_KIND = "blocked"
FAILURE_REASON = "executor_blocked"
#: Where an executor without the Control Plane tools writes its reason.
ENV_BLOCKED_FILE = "CONTROL_PLANE_BLOCKED_FILE"
#: ``systemStatusCategory`` of a task that waits for a person.
BLOCKED_CATEGORY = "blocked"
MAX_REASON_CHARS = 2000
NO_REASON = "the executor gave no reason"
#: The first words of the daemon's comment on a blocked task
#: (:func:`settle_blocked`); the next run of the same executor leaves it out of
#: the prompt (``comments.py``).
BLOCKED_COMMENT_PREFIX = "The run stopped without doing the work"
#: The same comment as daemons before universal-runner wrote it, only for
#: ``executor_blocked``: such comments stay on tasks and are left out as well.
LEGACY_BLOCKED_COMMENT_PREFIX = "The executor stopped without doing the work"
BLOCKED_COMMENT_PREFIXES = (BLOCKED_COMMENT_PREFIX, LEGACY_BLOCKED_COMMENT_PREFIX)


def _reason(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    # Durable and read elsewhere: no path of this host, bounded.
    return redact_local_paths(text)[:MAX_REASON_CHARS] if text else NO_REASON


async def blocked_reason(client: ControlPlaneClient, run_id: str) -> str | None:
    """The reason of the newest ``blocked`` checkpoint of the run, or None.

    A failure to read is raised, not taken for "not blocked": publishing a
    run as done on a guess is the failure this module exists to prevent.
    """
    checkpoints = await client.list_checkpoints(run_id)
    for checkpoint in reversed(list(checkpoints.get("items", []))):
        if checkpoint.get("kind") == CHECKPOINT_KIND:
            return _reason((checkpoint.get("data") or {}).get("reason"))
    return None


@contextlib.contextmanager
def blocked_file() -> Iterator[Path]:
    """A path outside the working copy for one turn, removed afterwards.

    Outside, so the signal is never committed with the work; under the
    system temporary directory, which a sandboxed executor may write to.
    """
    with tempfile.TemporaryDirectory(prefix="cp-blocked-") as directory:
        yield Path(directory) / "reason"


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("blocked file not readable: %s", exc)
        return ""


async def record_blocked_file(client: ControlPlaneClient, run_id: str, path: Path) -> bool:
    """Turn the executor's file into the ``blocked`` checkpoint; True if it wrote one.

    The file existing is the signal; its text is only the reason.
    """
    text = await asyncio.to_thread(_read, path)
    if text is None:
        return False
    await client.create_checkpoint(run_id, kind=CHECKPOINT_KIND, data={"reason": _reason(text)})
    return True


async def settle_blocked(
    client: ControlPlaneClient,
    task: dict[str, Any],
    run: dict[str, Any],
    claim: dict[str, Any],
    reason: str,
    *,
    failure_reason: str = FAILURE_REASON,
) -> None:
    """Hand a task whose run stopped without doing the work to a person.

    The run fails ``failure_reason`` and the task goes, under our claim, to
    the first ``blocked`` status its lifecycle allows from where it is — the
    verification stage's way to hand a task to a person (CP-ADR-0067 §5) —
    with the reason in a comment; then the claim is released, which leaves a
    status the claim did not set alone. Each step is best-effort after the
    run is failed: what could not be done is logged, and a task its lifecycle
    cannot block goes back to the queue as after any failure.
    """
    run_id, task_id = str(run["id"]), str(task["id"])
    logger.warning("%s stopped (%s): %s", task["publicId"], failure_reason, reason)
    await client.fail_run(run_id, failure_reason=failure_reason, output={"reason": reason})
    status: str | None = None
    try:
        targets = (await client.get_task_transitions(task_id)).get("targets") or []
        status = next(
            (
                str(t["status"])
                for t in targets
                if t.get("systemStatusCategory") == BLOCKED_CATEGORY and t.get("route") == "update"
            ),
            None,
        )
        if status is None:
            logger.warning("the lifecycle of %s has no blocked status", task["publicId"])
        else:
            fresh = await client.get_task(task_id)
            await client.update_task(
                task_id,
                expected_version=int(fresh["version"]),
                status=status,
                claim_id=str(claim["id"]),
                fencing_token=int(claim["fencingToken"]),
            )
    except ControlPlaneError as exc:
        logger.warning("could not block %s: %s", task["publicId"], exc.code)
        status = None
    with contextlib.suppress(ControlPlaneError):
        await client.add_task_comment(
            task_id,
            body=(
                f"{BLOCKED_COMMENT_PREFIX} ({failure_reason}): {reason}\n"
                + (
                    f"The task waits for a person in {status!r}; return it to work "
                    "to have it taken again."
                    if status is not None
                    else "The task could not be moved to a blocked status."
                )
            ),
            run_id=run_id,
        )
    with contextlib.suppress(ControlPlaneError):
        await client.release_claim(str(claim["id"]), reason=failure_reason)
