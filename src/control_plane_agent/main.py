"""``control-plane-agent`` — minimal autonomous reference harness.

Purpose: protocol symmetry, not autonomous intelligence. The daemon performs
the SAME discover → claim → run → artifact → complete cycle as the Claude
Code human harness, through the same client SDK against the same endpoints —
no special server path exists for agents.

The unit of pluggable behavior is an Adapter:
``execute(task, run, client, workspace)`` does the actual work and returns
artifact specs. The built-in ``echo`` adapter simply records what it saw —
enough for E2E protocol validation.

Execution workspace (ADR-0016 §5): when the agent is configured with a
workspace pool, every task is executed in its own git worktree on branch
``task/<publicId>``, and the resulting commit is registered as an artifact by
reference (``git:<sha>``). Without a pool the adapter gets ``workspace=None``
and the agent behaves exactly as before — the reference harness stays usable
for protocol tests that touch no files.

Restart recovery: on startup the agent consults /harness/context; a still-
live claim+run is finished honestly (fail with reason=restart_recovery) so
the task frees up deterministically — a reference policy, not the only one.

Skills (ADR-0056 §3, §5, ``skills.py``): with a skill executor configured the
same daemon runs skill invocations in its own workers, alongside Work
(``CONTROL_PLANE_SKILLS_CONCURRENCY`` at once; 0 — only while it has no Work),
and executes Work whose type declares ``execution = {skill, version}`` through
exactly one invocation instead of the adapter. Work of such a type is never
handed to the adapter: a daemon that cannot run the skill — or cannot read the
type to tell — leaves it for one that can.
"""

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any, Protocol

from control_plane_agent.review import (
    ReviewPolicy,
    build_review_task,
    parse_verdict,
    published_commit,
    review_policy_from_env,
    summary_of,
)
from control_plane_agent.skills import SkillExecutor, executor_from_environment
from control_plane_agent.supervision import (
    ExecutionStopped,
    RunSupervisor,
    SupervisionSettings,
    settle_stopped,
)
from control_plane_agent.workspace import (
    ARTIFACT_TYPE,
    CHECKPOINT_KIND,
    ExecutionWorkspacePool,
    Outcome,
    Workspace,
    WorkspaceBusyError,
    assert_portable,
    base_branch_of,
    parse_neighbours,
    redact_local_paths,
)
from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    HeartbeatRunner,
    PermissionDeniedError,
    SessionExpiredError,
    StaleClaimError,
    TransportError,
    resolve_credential,
)

logger = logging.getLogger("control_plane_agent")

#: How many available Work items one cycle looks at: an item this daemon must
#: not take (a skill it cannot run) should not hide the next one.
WORK_SCAN = 10


class _TypeUnreadable(Exception):
    """The task's type cannot be read, so whether a skill executes it is unknown."""


@dataclass(frozen=True)
class ArtifactSpec:
    type: str
    name: str
    uri: str | None = None
    content: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    """The pluggable execution engine of the autonomous harness.

    ``workspace`` is the isolated working copy for this task, or None when the
    agent runs without a pool. An adapter that touches files MUST work inside
    ``workspace.path`` and nowhere else: that is what keeps two concurrent
    tasks from writing into one copy.
    """

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
    ) -> list[ArtifactSpec]: ...


class EchoAdapter:
    """Trivial adapter: 'does the work' by describing it. For E2E tests."""

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None = None,
    ) -> list[ArtifactSpec]:
        await client.record_action(run["id"], action="echo.observe")
        return [
            ArtifactSpec(
                type="report",
                name=f"echo of {task['publicId']}",
                content={"echo": task["title"], "attempt": run["attempt"]},
            )
        ]


ADAPTERS: dict[str, type] = {"echo": EchoAdapter}

# Adapters that live outside the daemon are resolved by name and imported only
# when asked for: the reference harness must keep running on a host where no
# vendor CLI is installed, and an unconditional import would make Claude Code
# or Codex a hard dependency of protocol tests that never touch them.
EXTERNAL_ADAPTERS = ("claude-code", "codex")


def build_adapter(name: str) -> Adapter:
    """Instantiate an adapter by name. Raises LookupError if it is unknown."""
    factory = ADAPTERS.get(name)
    if factory is not None:
        return factory()  # type: ignore[no-any-return]
    if name == "claude-code":
        from control_plane_claude import adapter_from_environment

        return adapter_from_environment()
    if name == "codex":
        from control_plane_codex import adapter_from_environment as codex_adapter_from_environment

        return codex_adapter_from_environment()
    raise LookupError(name)


class Agent:
    def __init__(
        self,
        client: ControlPlaneClient,
        adapter: Adapter,
        *,
        poll_interval: float = 5.0,
        workspace_id: str | None = None,
        project_id: str | None = None,
        include_subprojects: bool = False,
        heartbeat_interval: float = 60.0,
        max_cycles: int | None = None,
        workspaces: ExecutionWorkspacePool | None = None,
        only_assigned: bool = False,
        review_policy: ReviewPolicy | None = None,
        review_type: str = "code-review",
        skills: SkillExecutor | None = None,
        supervision: SupervisionSettings | None = None,
    ) -> None:
        self.client = client
        self.adapter = adapter
        # While the adapter works the daemon watches the run: a cancel request
        # or a run without progress stops the adapter (supervision.py).
        self.supervision = supervision or SupervisionSettings()
        # The skill adapter (ADR-0056 §5): runs invocations when there is no
        # Work, and Work whose type is executed by a skill.
        self.skills = skills
        self._executions: dict[str, dict[str, Any] | None] = {}
        # Type id -> does the version declare work after completion
        # (CP-ADR-0061, amendment 2026-09-25)? Versions are immutable.
        self._completion_work: dict[str, bool] = {}
        self._unreadable_types: set[str] = set()
        self._session_lock = asyncio.Lock()
        self._skills_stop = asyncio.Event()
        # Auto-review (review.py): with a policy, every finished task of a
        # reviewed type with a published branch spawns a code-review task for
        # the configured reviewer. review_type is what THIS runner recognises
        # as a review when it is the reviewer itself — then the verdict in the
        # summary is copied into the task's fields.
        self.review_policy = review_policy
        self.review_type = review_type
        self.poll_interval = poll_interval
        self.workspace_id = workspace_id
        self.project_id = project_id
        self.include_subprojects = include_subprojects
        self.heartbeat_interval = heartbeat_interval
        self.max_cycles = max_cycles
        # Execution workspaces are optional: an adapter that touches no files
        # (protocol tests, echo) needs no working copy at all.
        self.workspaces = workspaces
        # Take only work addressed to this Principal. Off by default, because
        # the reference harness exists to prove the protocol and an unassigned
        # queue is the simplest way to do that — but any runner doing real work
        # wants it on: "claimable by me" and "meant for me" are not the same
        # question, and only the second is a decision somebody made.
        self.only_assigned = only_assigned
        self.session_id: str | None = None
        self._session_heartbeats: HeartbeatRunner | None = None
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    # -- lifecycle -------------------------------------------------------------

    async def _open_session(self) -> str:
        session = await self.client.open_session(
            client_name="control-plane-agent",
            client_version="0.4.0",
            harness_type="autonomous-agent",
            capabilities=[
                "resume",
                "checkpoints",
                "artifacts.publish",
                *(self.skills.capabilities if self.skills is not None else []),
            ],
            environment={"adapter": type(self.adapter).__name__},
        )
        self.session_id = str(session["id"])
        # Keep the session lease alive across idle polls, not only during a run
        # (an idle daemon whose session expires cannot claim anything).
        if self._session_heartbeats is not None:
            await self._session_heartbeats.stop()
        self._session_heartbeats = HeartbeatRunner(
            self.client, session_id=self.session_id, interval_seconds=self.heartbeat_interval
        )
        self._session_heartbeats.start()
        return self.session_id

    async def _ensure_session(self) -> str:
        """Return a live session, reopening if the current one was lost.

        Serialized: the work cycle and the skill workers share one session,
        and two of them noticing its loss must not open two.
        """
        async with self._session_lock:
            if self.session_id is None:
                return await self._open_session()
            if self._session_heartbeats is not None and self._session_heartbeats.error is not None:
                logger.info("session lease lost; reopening")
                return await self._open_session()
            return self.session_id

    async def recover(self) -> None:
        """Startup recovery: reap ONLY orphaned work of this principal.

        A run/claim is orphaned when its backing session is no longer live —
        i.e. left by a crashed predecessor. Work backed by a still-live session
        may belong to a concurrent sibling instance of the same principal and
        MUST NOT be touched; its lease will expire on its own if it too died.
        """
        context = await self.client.get_context()
        live_sessions = {s["id"] for s in context["activeSessions"]}
        for run in context["activeRuns"]:
            if run["sessionId"] in live_sessions:
                continue
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run["id"], failure_reason="restart_recovery")
                logger.info("recovered: failed orphaned run %s", run["id"])
        for claim in context["activeClaims"]:
            if claim["sessionId"] in live_sessions:
                continue
            with contextlib.suppress(ControlPlaneError):
                await self.client.release_claim(claim["id"], reason="restart_recovery")
                logger.info("recovered: released orphaned claim %s", claim["id"])

    async def _skill_worker(self, skills: SkillExecutor) -> None:
        """Take skill invocations one after another until the daemon stops.

        Runs beside the work cycle, so a long Work does not hold the queue;
        an invocation in flight is finished (or its lease lost), not dropped.
        """
        while not self._skills_stop.is_set():
            try:
                worked = await skills.run_once(await self._ensure_session())
            except ControlPlaneError as exc:
                logger.warning("skill worker: %s", exc)
                worked = False
            except Exception:
                logger.exception("skill worker failed; continuing")
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._skills_stop.wait(), self.poll_interval)

    async def run_forever(self) -> None:
        await self.recover()
        await self._open_session()
        cycles = 0
        skills = self.skills
        workers = (
            [asyncio.create_task(self._skill_worker(skills)) for _ in range(skills.concurrency)]
            if skills is not None
            else []
        )
        try:
            while not self._stop.is_set():
                if self.max_cycles is not None and cycles >= self.max_cycles:
                    return
                cycles += 1
                try:
                    worked = await self.run_once()
                except TransportError as exc:
                    logger.warning("transport failure, backing off: %s", exc)
                    worked = False
                except ControlPlaneError as exc:
                    logger.warning("cycle error: %s", exc)
                    worked = False
                if not worked:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), self.poll_interval)
        finally:
            self._skills_stop.set()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            if self._session_heartbeats is not None:
                await self._session_heartbeats.stop()
            if self.session_id is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.close_session(self.session_id)

    # -- one work cycle --------------------------------------------------------

    async def _execution_of(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """``execution`` of the task's type version, read once per version.

        A principal without ``task_types.read`` cannot tell whether a skill
        executes the task, and handing it to the adapter on a guess would run
        skill work as code: ``_TypeUnreadable``, the task is left alone. Not
        cached, so a right granted later takes effect; logged once per type.
        """
        type_id = str(task.get("typeId") or "")
        if not type_id:
            return None
        if type_id not in self._executions:
            try:
                task_type = await self.client.get_task_type(type_id)
            except PermissionDeniedError as exc:
                if type_id not in self._unreadable_types:
                    self._unreadable_types.add(type_id)
                    logger.warning(
                        "no task_types.read: leaving %s alone, its type %s may be "
                        "executed by a skill",
                        task.get("publicId"),
                        type_id,
                    )
                raise _TypeUnreadable(type_id) from exc
            self._unreadable_types.discard(type_id)
            self._executions[type_id] = task_type.get("execution")
            self._completion_work[type_id] = bool(task_type.get("completionSchema"))
        return self._executions[type_id]

    async def _takes(self, execution: dict[str, Any] | None) -> bool:
        """Ordinary work goes to the adapter; skill work only to a capable executor."""
        if execution is None:
            return True
        if self.skills is None:
            return False
        try:
            skill = await self.skills.describe(f"{execution['skill']}@{execution['version']}")
        except ControlPlaneError as exc:
            logger.info("skill %s not readable: %s", execution.get("skill"), exc.code)
            return False
        return self.skills.can_execute(skill)

    async def run_once(self) -> bool:
        """Discover, take ONE task through the full cycle. True if work done.

        Without Work, a daemon whose skill executor has no workers of its own
        (``concurrency = 0``) takes one skill invocation instead.
        """
        session_id = await self._ensure_session()
        page = await self.client.list_available_work(
            limit=WORK_SCAN,
            workspace_id=self.workspace_id,
            include_descendants=True,
            project_id=self.project_id,
            include_subprojects=self.include_subprojects,
            assigned_to_me=self.only_assigned,
        )
        task: dict[str, Any] | None = None
        execution: dict[str, Any] | None = None
        for item in page["items"]:
            try:
                execution = await self._execution_of(item)
            except _TypeUnreadable:
                continue
            if await self._takes(execution):
                task = item
                break
        if task is None:
            if self.skills is not None and self.skills.concurrency == 0:
                return await self.skills.run_once(session_id)
            return False

        # Autonomous policy: claim automatically (the human harness would ask).
        try:
            claim = await self.client.claim_task(
                task["id"], session_id, intent="autonomous-agent auto"
            )
        except SessionExpiredError:
            # Our session died between the poll and the claim; drop it so the
            # next cycle reopens, and treat this cycle as no-op.
            await self._open_session()
            return False
        except ControlPlaneError as exc:
            logger.info("claim lost race for %s: %s", task["publicId"], exc.code)
            return False

        heartbeats = HeartbeatRunner(
            self.client,
            session_id=session_id,
            claim_id=str(claim["id"]),
            interval_seconds=self.heartbeat_interval,
        )
        heartbeats.start()
        workspace: Workspace | None = None
        # "suspended" is part of Outcome but never assigned here: today every
        # non-success path ends in fail_run, and the workspace is released the
        # same way ("failed") regardless. It's reserved for graceful
        # degradation (approval gate, budget limit, cancellation) that doesn't
        # exist yet — those will set outcome = "suspended" without changing
        # this release logic.
        outcome: Outcome = "failed"
        try:
            run = await self.client.start_run(
                task["id"],
                claim_id=str(claim["id"]),
                fencing_token=int(claim["fencingToken"]),
            )
            if execution is not None:
                assert self.skills is not None
                return await self._run_skill_work(
                    task, run, execution, session_id, heartbeats, self.skills
                )
            try:
                try:
                    workspace = await self._open_workspace(task, run)
                except WorkspaceBusyError as exc:
                    # Another process owns this copy. Honest failure for now;
                    # once AR-5 lands this becomes checkpoint → suspend.
                    logger.warning("workspace busy for %s: %s", task["publicId"], exc)
                    with contextlib.suppress(ControlPlaneError):
                        await self.client.fail_run(str(run["id"]), failure_reason="workspace_busy")
                    return False
                supervisor = RunSupervisor(self.client, str(run["id"]), self.supervision)
                artifacts = await supervisor.run(
                    self.adapter.execute(task, run, self.client, workspace)
                )
                if heartbeats.error is not None:
                    # The lease died while the adapter worked: the server would
                    # fence us anyway, so stop before writing results.
                    logger.warning(
                        "lease lost during %s (%s); aborting",
                        task["publicId"],
                        heartbeats.error.code,
                    )
                    with contextlib.suppress(ControlPlaneError):
                        await self.client.fail_run(str(run["id"]), failure_reason="lease_lost")
                    return False
                if workspace is not None:
                    artifacts = [*artifacts, *await self._commit_evidence(task, run, workspace)]
                for spec in artifacts:
                    # Nothing leaves the host that names this host: an adapter
                    # that put a local path or a credential in an artifact is
                    # stopped here, not discovered later by a reader.
                    assert_portable(
                        {
                            "name": spec.name,
                            "uri": spec.uri,
                            "content": spec.content,
                            "metadata": spec.metadata,
                        },
                        where="artifact",
                    )
                    await self.client.create_artifact(
                        type=spec.type,
                        name=spec.name,
                        task_ref=task["id"],
                        run_id=str(run["id"]),
                        uri=spec.uri,
                        content=spec.content,
                        metadata=spec.metadata,
                    )
                await self._record_verdict(task, artifacts, claim)
                await self.client.succeed_run(str(run["id"]))
                outcome = "succeeded"
                logger.info("completed %s", task["publicId"])
                await self._request_review(task, artifacts)
                return True
            except ExecutionStopped as stop:
                # Asked to stop, or stuck: the adapter is stopped; close the run
                # once and let the task go (to the rule's decision, or back to
                # the queue).
                logger.warning("stopped %s: %s", task["publicId"], stop.reason)
                await settle_stopped(
                    self.client,
                    run_id=str(run["id"]),
                    claim_id=str(claim["id"]),
                    fencing_token=int(claim["fencingToken"]),
                    stop=stop,
                )
                return True
            except StaleClaimError:
                # Ownership lost mid-flight: stop writing, report honestly.
                logger.warning("ownership of %s lost; aborting", task["publicId"])
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(str(run["id"]), failure_reason="ownership_lost")
                return False
            except Exception as exc:
                # failure_reason is durable and read in other environments, and
                # this handler catches anything an adapter may raise — including
                # exceptions whose text this package never composed. Redacting
                # here covers what the workspace guard cannot see.
                reason = redact_local_paths(f"{type(exc).__name__}: {exc}")[:500]
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(str(run["id"]), failure_reason=reason)
                raise
        finally:
            if workspace is not None and self.workspaces is not None:
                await asyncio.to_thread(self.workspaces.release, workspace, outcome)
            await heartbeats.stop()

    async def _run_skill_work(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        execution: dict[str, Any],
        session_id: str,
        heartbeats: HeartbeatRunner,
        skills: SkillExecutor,
    ) -> bool:
        """Work executed by a skill (ADR-0056 §3): one invocation, then the run.

        The ``skill_result`` artifact is written by the core when the call
        succeeds; the run carries only references to it. No workspace, no
        adapter, no review: the skill is the whole execution, and what it did
        is decided by its contract, not by this daemon.
        """
        run_id = str(run["id"])
        try:
            invocation = await skills.execute_work(
                task, run, execution, session_id, alive=lambda: heartbeats.error is None
            )
        except StaleClaimError:
            logger.warning("ownership of %s lost; aborting", task["publicId"])
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run_id, failure_reason="ownership_lost")
            return False
        except ControlPlaneError as exc:
            # The core refused the call (inputs, rights, basis): nothing ran.
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(
                    run_id, failure_reason=f"skill_invocation_rejected: {exc.code}"[:500]
                )
            logger.warning("skill call for %s rejected: %s", task["publicId"], exc.code)
            return False
        except Exception as exc:
            reason = redact_local_paths(f"{type(exc).__name__}: {exc}")[:500]
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run_id, failure_reason=reason)
            raise
        summary = {
            "skillInvocationId": str(invocation["id"]),
            "skill": f"{execution['skill']}@{execution['version']}",
            "status": invocation.get("status"),
            "artifactId": invocation.get("artifactId"),
        }
        if heartbeats.error is not None:
            logger.warning("lease lost during %s; aborting", task["publicId"])
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(run_id, failure_reason="lease_lost", output=summary)
            return False
        if invocation.get("status") != "succeeded":
            code = (invocation.get("error") or {}).get("code") or invocation.get("status")
            with contextlib.suppress(ControlPlaneError):
                await self.client.fail_run(
                    run_id,
                    failure_reason=f"skill_invocation_{invocation.get('status')}: {code}"[:500],
                    output={**summary, "error": invocation.get("error")},
                )
            logger.info("skill work %s failed: %s", task["publicId"], code)
            return False
        await self.client.succeed_run(run_id, output=summary)
        logger.info("completed %s through %s", task["publicId"], summary["skill"])
        return True

    # -- execution workspace ---------------------------------------------------

    async def _request_review(self, task: dict[str, Any], artifacts: list[ArtifactSpec]) -> None:
        """Coder side of auto-review: spawn a code-review task for the reviewer.

        Runs AFTER the run succeeded on purpose: a review that could not be
        requested must never undo finished work, so failures here are logged
        and left in a comment for a human, not raised.
        """
        policy = self.review_policy
        if policy is None or not policy.applies_to(task):
            return
        if await self._type_declares_completion_work(task):
            # The type files its own follow-up once the task is completed, and
            # it already has (in the same transaction as succeed_run): a review
            # from here would be the second one.
            logger.info(
                "type of %s declares work after completion; review left to it", task["publicId"]
            )
            return
        commit = published_commit(artifacts)
        if commit is None:
            logger.info("no published commit for %s; review not requested", task["publicId"])
            return
        try:
            review = await self._create_review(build_review_task(task, commit, policy))
            await self.client.add_task_relation(
                str(review["id"]), to_task=str(task["id"]), relation_type="spawned_by"
            )
            if policy.human:
                await self._request_review_approval(review, task, commit, policy)
            logger.info("review %s requested for %s", review["publicId"], task["publicId"])
        except ControlPlaneError as exc:
            logger.warning("could not request review for %s: %s", task["publicId"], exc.code)
            with contextlib.suppress(ControlPlaneError):
                await self.client.add_task_comment(
                    str(task["id"]),
                    body=(
                        f"Автоматическое ревью не заведено ({exc.code}); "
                        "ревью нужно назначить руками."
                    ),
                )

    async def _type_declares_completion_work(self, task: dict[str, Any]) -> bool:
        """Does the task's type version declare ``completionSchema``?

        Read with the type (see :meth:`_execution_of`). Unreadable — the
        behaviour before the declaration existed: a review missing is worse
        than a duplicate one, which a human can see and close.
        """
        type_id = str(task.get("typeId") or "")
        if not type_id:
            return False
        if type_id not in self._completion_work:
            try:
                task_type = await self.client.get_task_type(type_id)
            except ControlPlaneError as exc:
                logger.warning(
                    "type %s of %s unreadable (%s); falling back to the runner's review",
                    type_id,
                    task.get("publicId"),
                    exc.code,
                )
                return False
            self._completion_work[type_id] = bool(task_type.get("completionSchema"))
        return self._completion_work[type_id]

    async def _create_review(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Create the review task; without its custom fields if the type refuses them.

        A review type whose ``field_schema`` predates the merge fields
        (``repository``, ``branch``, ``commit``, ``targetBranch``) and forbids
        extra ones would otherwise refuse every review. The review is still
        worth having: a human can read the branch in the description, and an
        outcome that needs the fields fails visibly (``unresolved_expression``)
        instead of merging by guess (CP-ADR-0061 §11).
        """
        try:
            return await self.client.create_task(**spec)
        except ControlPlaneError as exc:
            if exc.code != "custom_fields_invalid" or "custom_fields" not in spec:
                raise
            logger.warning(
                "review type refuses the merge fields (%s); review created without them", exc.code
            )
            return await self.client.create_task(
                **{k: v for k, v in spec.items() if k != "custom_fields"}
            )

    async def _request_review_approval(
        self,
        review: dict[str, Any],
        task: dict[str, Any],
        commit: dict[str, Any],
        policy: ReviewPolicy,
    ) -> None:
        """Human review mode: the gate approval on the review task IS the verdict.

        Keyed by the review task, so a retried run never opens a second
        decision. A failure leaves the review task in place and says so on it:
        the human can still review, only the gate is missing.
        """
        try:
            await self.client.request_approval(
                task_ref=str(review["id"]),
                assigned_principal_id=policy.reviewer_principal_id,
                gate=True,
                comment=(
                    f"Code review {task['publicId']}: ветка {commit['branch']}, "
                    f"коммит {commit['commit']}. approve — принято, reject — нужны правки."
                ),
                idempotency_key=f"code-review-approval:{review['id']}",
            )
        except ControlPlaneError as exc:
            logger.warning(
                "could not request review approval for %s: %s", review["publicId"], exc.code
            )
            with contextlib.suppress(ControlPlaneError):
                await self.client.add_task_comment(
                    str(review["id"]),
                    body=(
                        f"Approval ревью не заведён ({exc.code}); решение оформите "
                        "approval'ом вручную или комментарием."
                    ),
                )

    async def _record_verdict(
        self, task: dict[str, Any], artifacts: list[ArtifactSpec], claim: dict[str, Any]
    ) -> None:
        """Reviewer side of auto-review: verdict from the summary into task fields.

        Before succeed_run, because a completed task may refuse field updates,
        and WITH our claim: the server rejects a PATCH on a claimed task unless
        the holder presents claimId + fencingToken (seen as ``task_claimed`` on
        the first BidOps review). A missing or unparsable verdict is reported
        in a comment so a human knows the review needs reading.
        """
        if task.get("typeKey") != self.review_type:
            return
        verdict, notes = parse_verdict(summary_of(artifacts))
        try:
            if verdict is None:
                await self.client.add_task_comment(
                    str(task["id"]),
                    body=(
                        "Ревьюер не оформил вердикт первой строкой summary — "
                        "прочитать отчёт и выставить verdict руками."
                    ),
                )
                return
            fresh = await self.client.get_task(str(task["id"]))
            fields = dict(fresh.get("customFields") or {})
            fields.update({"verdict": verdict, "notes": notes})
            await self.client.update_task(
                str(task["id"]),
                expected_version=int(fresh["version"]),
                custom_fields=fields,
                claim_id=str(claim["id"]),
                fencing_token=int(claim["fencingToken"]),
            )
            logger.info("verdict for %s: %s", task["publicId"], verdict)
        except ControlPlaneError as exc:
            logger.warning("could not record verdict for %s: %s", task["publicId"], exc.code)

    async def _open_workspace(self, task: dict[str, Any], run: dict[str, Any]) -> Workspace | None:
        """Take this task's working copy and record it durably.

        The checkpoint is what makes the copy survive a restart: a later run of
        the same task finds the branch here instead of forking a second copy.
        """
        if self.workspaces is None:
            return None
        workspace = await asyncio.to_thread(
            self.workspaces.acquire, task["publicId"], base_branch_of(task)
        )
        await self.client.create_checkpoint(
            str(run["id"]), kind=CHECKPOINT_KIND, data=workspace.checkpoint_data
        )
        logger.info(
            "workspace %s on %s (%s)",
            workspace.key,
            workspace.branch,
            "reused" if workspace.reused else "created",
        )
        return workspace

    async def _commit_evidence(
        self, task: dict[str, Any], run: dict[str, Any], workspace: Workspace
    ) -> list[ArtifactSpec]:
        """Turn the working copy into evidence: a commit, referenced not copied.

        The branch is then published, when the pool has a remote, so the work
        can be reviewed where code is normally reviewed. Publishing is the
        DAEMON's job and not the agent's for the same reason completion is: this
        process holds the claim and its fencing token, so what it publishes is
        attributable to the run that produced it.
        """
        sha = await asyncio.to_thread(workspace.commit, f"{task['publicId']}: {task['title']}")
        if sha is None:
            logger.info("no changes in %s; nothing to commit", workspace.key)
            return []

        remote = self.workspaces.push_remote if self.workspaces is not None else ""
        published = False
        # Where the branch lives and what it is meant to be merged into: an
        # approval outcome of the review hands both to a merge skill
        # (CP-ADR-0061), which cannot guess them.
        location: dict[str, str] = {}
        pool = self.workspaces
        if remote and pool is not None:
            published = await asyncio.to_thread(workspace.push, remote)
            logger.info(
                "%s %s", "published" if published else "could not publish", workspace.branch
            )
            repository = await asyncio.to_thread(pool.push_remote_url)
            location["repository"] = repository or ""
            # The branch the copy was cut from: a task's feature branch
            # (``customFields.baseBranch``) is where its work is merged back.
            location["targetBranch"] = workspace.base_branch or await asyncio.to_thread(
                lambda: pool.base_branch
            )

        await self.client.create_checkpoint(
            str(run["id"]),
            kind=CHECKPOINT_KIND,
            data={**workspace.checkpoint_data, "head": sha, "published": published},
        )
        return [
            ArtifactSpec(
                type=ARTIFACT_TYPE,
                name=f"{workspace.branch}@{sha[:12]}",
                uri=workspace.artifact_uri(sha),
                metadata={
                    "branch": workspace.branch,
                    "commit": sha,
                    "workspaceKey": workspace.key,
                    # A reviewer needs to know whether the branch is actually in
                    # the forge: a commit that exists only on the runner cannot
                    # be reviewed, and silence about that would waste their time.
                    "published": published,
                    **{k: v for k, v in location.items() if v},
                },
            )
        ]


def _workspace_pool_from_env() -> ExecutionWorkspacePool | None:  # pragma: no cover - wiring
    """Build the workspace pool if the runner was given a repository to work in.

    Note the naming: ``CONTROL_PLANE_AGENT_WORKSPACE`` is the Control Plane
    Workspace to take tasks from, an entirely different thing from the local
    working copies configured here.
    """
    origin = os.environ.get("CONTROL_PLANE_AGENT_REPO", "")
    root = os.environ.get("CONTROL_PLANE_AGENT_WORKTREE_ROOT", "")
    if not origin or not root:
        return None
    return ExecutionWorkspacePool(
        origin,
        root,
        base_ref=os.environ.get("CONTROL_PLANE_AGENT_BASE_REF", "HEAD"),
        keep_on_success=os.environ.get("CONTROL_PLANE_AGENT_KEEP_WORKSPACES") == "1",
        max_workspaces=int(os.environ.get("CONTROL_PLANE_AGENT_MAX_WORKSPACES", "8")),
        # Named, not a boolean: a runner may legitimately publish somewhere
        # other than the remote it clones from, and an empty value keeps the
        # work local — the default, since publishing needs a credential.
        push_remote=os.environ.get("CONTROL_PLANE_AGENT_PUSH_REMOTE", ""),
        # Neighbours the copy must build against, and the superproject that
        # says at which revision each of them. Both are configuration of the
        # deployment: which repositories sit on this runner is not something
        # the agent may decide per task.
        repo_dir=os.environ.get("CONTROL_PLANE_AGENT_REPO_DIR", ""),
        neighbours=parse_neighbours(os.environ.get("CONTROL_PLANE_AGENT_NEIGHBOURS", "")),
        superproject=os.environ.get("CONTROL_PLANE_AGENT_SUPERPROJECT") or None,
        superproject_ref=os.environ.get("CONTROL_PLANE_AGENT_SUPERPROJECT_REF", "HEAD"),
        superproject_remote=os.environ.get("CONTROL_PLANE_AGENT_SUPERPROJECT_REMOTE", ""),
    )


def main() -> int:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = os.environ.get("CONTROL_PLANE_SERVER", "").rstrip("/")
    if not server:
        print("control-plane-agent requires CONTROL_PLANE_SERVER")
        return 2
    # The same resolution the human harness uses: an IAM identity first, then a
    # legacy API key. Reading CONTROL_PLANE_API_KEY directly would have sent the
    # Platform Access Token itself as a Bearer — a PAT is exchanged for an
    # audience-bound token, never presented — so a runner on an IAM-only server
    # could not authenticate at all.
    credential = resolve_credential(server)
    if credential is None:
        print(
            f"control-plane-agent has no credentials for {server}: set "
            "CONTROL_PLANE_IAM_URL and CONTROL_PLANE_IAM_TENANT with a Platform "
            "Access Token in IAM_PLATFORM_ACCESS_TOKEN, or CONTROL_PLANE_API_KEY "
            "where legacy keys are still enabled"
        )
        return 2
    adapter_name = os.environ.get("CONTROL_PLANE_AGENT_ADAPTER", "echo")
    try:
        adapter = build_adapter(adapter_name)
    except LookupError:
        available = sorted({*ADAPTERS, *EXTERNAL_ADAPTERS})
        print(f"unknown adapter '{adapter_name}' (available: {available})")
        return 2
    except ImportError as exc:
        print(f"adapter '{adapter_name}' is not installed on this runner: {exc}")
        return 2

    async def _run() -> None:
        async with ControlPlaneClient(server, credential) as client:
            try:
                skills = executor_from_environment(client)
            except ValueError as exc:
                logger.error("skill executor misconfigured: %s", exc)
                raise SystemExit(2) from exc
            agent = Agent(
                client,
                adapter,
                skills=skills,
                supervision=SupervisionSettings.from_environment(),
                workspaces=_workspace_pool_from_env(),
                workspace_id=os.environ.get("CONTROL_PLANE_AGENT_WORKSPACE") or None,
                project_id=os.environ.get("CONTROL_PLANE_AGENT_PROJECT") or None,
                include_subprojects=os.environ.get("CONTROL_PLANE_AGENT_SUBPROJECTS") == "1",
                only_assigned=os.environ.get("CONTROL_PLANE_AGENT_ONLY_ASSIGNED") == "1",
                poll_interval=float(os.environ.get("CONTROL_PLANE_AGENT_POLL", "5")),
                review_policy=review_policy_from_env(),
                review_type=os.environ.get("CONTROL_PLANE_AGENT_REVIEW_TYPE") or "code-review",
            )
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, agent.request_stop)
            await agent.run_forever()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
