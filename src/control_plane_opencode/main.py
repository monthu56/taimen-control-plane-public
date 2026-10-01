"""OpenCode harness adapter (ADR-0041).

Speaks the Harness Protocol to the Control Plane through the official SDK and
drives an ``opencode serve`` process over its documented HTTP API. It is a
CLIENT: no database access, no core changes, and nothing product-specific
crosses into the Control Plane — ``harness.type = "opencode"`` is a value of an
existing column and ``skills.protocol.opencode`` already exists.

Continuity is server-side: the OpenCode session id and the last message id are
written to a Run Checkpoint, so after a restart the adapter resumes the SAME
OpenCode session instead of starting a fresh one.

The environment of a run (``env`` of ``.agents/runner.yaml``, universal-runner
FR-018) reaches OpenCode's tools only through the server process, so a run
with one needs a server of its own (``server.py``): given the binary, the
adapter starts ``opencode serve`` for such a run and stops it after; a run
without an environment uses the shared server. A run with an environment and
only a shared server fails instead of losing it.

Test services of ``runner.yaml`` (universal-runner U013) are asked of the node
by the runner daemon only, for the working copy of the task's base; this
harness has no such copy and asks nothing. A run in a repository whose
``runner.yaml`` declares services, with no source of the run environment,
stops ``blocked`` (``test_services_not_offered``) instead of going on without
them; with ``CONTROL_PLANE_AGENT_SERVICES=host`` the host provides them and
nothing is checked.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from control_plane_agent.blocked import settle_blocked
from control_plane_agent.comments import own_principal_id, with_comments
from control_plane_agent.instructions import (
    build_prompt,
    prompt_file_from_environment,
    read_conventions,
)
from control_plane_agent.runner_config import RUNNER_CONFIG_PATH, ServiceSpec, run_environment
from control_plane_agent.services import (
    ENV_SERVICES_MODE,
    NOT_OFFERED,
    ServicesBlocked,
    declared_services_at,
    services_mode,
)
from control_plane_agent.supervision import (
    ExecutionStopped,
    RunSupervisor,
    SupervisionSettings,
    settle_stopped,
)
from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    HeartbeatRunner,
    SessionExpiredError,
    StaleClaimError,
    resolve_api_key,
)
from control_plane_client.credentials import removed_environment_variables
from control_plane_opencode.opencode import (
    OpenCodeClient,
    OpenCodeError,
    reply_message_id,
    reply_text,
)
from control_plane_opencode.server import OpenCodeServer

logger = logging.getLogger("control_plane_opencode")

HARNESS_TYPE = "opencode"
HARNESS_VERSION = "0.5.0"
CHECKPOINT_KIND = "opencode.session"
HARNESS_CAPABILITIES = [
    "resume",
    "checkpoints",
    "artifacts.publish",
    "skills.protocol.opencode",
]

SYSTEM_PROMPT = (
    "You are executing one task from a Control Plane work queue. The task "
    "description and the authoritative operational context are given below. "
    "Do the work, then summarize what you changed and what remains."
)

#: Where a run's environment comes from: ``(task, run)`` to the variables.
RunEnvironmentSource = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Mapping[str, str]]]
#: The services ``runner.yaml`` of the repository worked in declares; raises
#: :class:`ServicesBlocked` for a file that does not parse.
DeclaredServices = Callable[[], Mapping[str, ServiceSpec]]


class OpenCodeAdapter:
    # Agent conventions, the fourth instructions layer (CP-ADR-0066).
    prompt_file: Path | None = None

    def __init__(
        self,
        client: ControlPlaneClient,
        opencode: OpenCodeClient,
        *,
        poll_interval: float = 5.0,
        workspace_id: str | None = None,
        project_id: str | None = None,
        include_subprojects: bool = False,
        heartbeat_interval: float = 60.0,
        model: str | None = None,
        agent: str | None = None,
        max_cycles: int | None = None,
        prompt_file: Path | None = None,
        supervision: SupervisionSettings | None = None,
        server: OpenCodeServer | None = None,
        run_env: RunEnvironmentSource | None = None,
        declared_services: DeclaredServices | None = None,
    ) -> None:
        self.client = client
        self.opencode = opencode
        self.poll_interval = poll_interval
        self.workspace_id = workspace_id
        self.project_id = project_id
        self.include_subprojects = include_subprojects
        self.heartbeat_interval = heartbeat_interval
        self.model = model
        self.agent = agent
        self.max_cycles = max_cycles
        self.prompt_file = prompt_file
        # Read once: its own "blocked" comments stay out of the prompt.
        self._own_principal: str | None = None
        # A cancel request or a run without progress stops the prompt
        # (control_plane_agent.supervision), as for every other adapter.
        self.supervision = supervision or SupervisionSettings()
        # With a server launcher a run with an environment gets its own
        # ``opencode serve``; every other run uses ``opencode``, the shared one.
        self.server = server
        self.run_env = run_env
        # Read before a run without ``run_env`` starts: declared services
        # nobody asks for stop the run instead of letting it go without them.
        self.declared_services = declared_services
        self.session_id: str | None = None
        self.session_heartbeats: HeartbeatRunner | None = None
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    # -- session ---------------------------------------------------------------

    async def _open_session(self) -> None:
        session = await self.client.open_session(
            client_name="control-plane-opencode",
            client_version=HARNESS_VERSION,
            harness_type=HARNESS_TYPE,
            harness_version=HARNESS_VERSION,
            capabilities=HARNESS_CAPABILITIES,
            environment={"cwd": os.getcwd()},
        )
        self.session_id = session["id"]
        if self.session_heartbeats is not None:
            await self.session_heartbeats.stop()
        self.session_heartbeats = HeartbeatRunner(
            self.client, session_id=session["id"], interval_seconds=self.heartbeat_interval
        )
        self.session_heartbeats.start()

    async def recover(self) -> None:
        """Release work stranded by a previous process (same rule as the agent)."""
        context = await self.client.get_context()
        live = {s["id"] for s in context.get("activeSessions", [])}
        for run in context.get("activeRuns", []):
            if run.get("sessionId") not in live:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(run["id"], failure_reason="restart_recovery")
        for claim in context.get("activeClaims", []):
            if claim.get("sessionId") not in live:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.release_claim(claim["id"], reason="restart_recovery")

    # -- one task --------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _opencode_for(self, env: Mapping[str, str]) -> AsyncIterator[OpenCodeClient]:
        """The OpenCode server of this run: its own for a run with an environment.

        A run without one needs no server of its own and gets the shared one.
        """
        if env and self.server is not None:
            async with self.server.running(env) as opencode:
                yield opencode
            return
        if env:
            # A shared server would hand the variables to every later run, or
            # the run would go without them; neither is honest.
            raise OpenCodeError(
                "the run has its own environment, but the OpenCode server is shared; "
                "set CONTROL_PLANE_OPENCODE_BINARY to start a server per run"
            )
        yield self.opencode

    async def _run_environment(self, task: dict[str, Any], run: dict[str, Any]) -> dict[str, str]:
        if self.run_env is None:
            await self._check_services()
            return {}
        return run_environment(await self.run_env(task, run))

    async def _check_services(self) -> None:
        """:class:`ServicesBlocked` when ``runner.yaml`` declares services this run cannot get."""
        if self.declared_services is None:
            return
        services = await asyncio.to_thread(self.declared_services)
        if services:
            raise ServicesBlocked(
                NOT_OFFERED,
                f"{RUNNER_CONFIG_PATH} asks for services ({', '.join(sorted(services))}), "
                "but the OpenCode harness does not ask the node for them: run the task "
                "on a runner daemon whose executor kind has services: true, or set "
                f"{ENV_SERVICES_MODE}=host where the host provides them",
            )

    async def _resume_opencode_session(
        self, opencode: OpenCodeClient, run_id: str, title: str
    ) -> tuple[str, bool]:
        """Reuse the OpenCode session recorded on this run, else create one."""
        try:
            checkpoints = await self.client.list_checkpoints(run_id)
        except ControlPlaneError:
            checkpoints = {"items": []}
        for checkpoint in reversed(checkpoints.get("items", [])):
            if checkpoint.get("kind") != CHECKPOINT_KIND:
                continue
            candidate = (checkpoint.get("data") or {}).get("openCodeSessionId")
            if isinstance(candidate, str) and await opencode.session_exists(candidate):
                return candidate, True
        return await opencode.create_session(title=title), False

    def _build_prompt(self, task: dict[str, Any], context: dict[str, Any]) -> str:
        # SYSTEM_PROMPT travels as OpenCode's system message; the rest is the
        # prompt every adapter builds (CP-ADR-0066).
        return build_prompt(task, context, conventions=read_conventions(self.prompt_file))

    async def run_once(self) -> bool:
        """Claim one task, drive OpenCode through it, report the outcome."""
        if self.session_id is None:
            await self._open_session()
        assert self.session_id is not None

        page = await self.client.list_available_work(
            limit=1,
            workspace_id=self.workspace_id,
            include_descendants=True,
            project_id=self.project_id,
            include_subprojects=self.include_subprojects,
        )
        items = page.get("items", [])
        if not items:
            return False
        task = items[0]

        try:
            claim = await self.client.claim_task(
                task["id"], self.session_id, intent="opencode adapter"
            )
        except SessionExpiredError:
            await self._open_session()
            return False
        except ControlPlaneError as exc:
            logger.info("claim lost the race: %s", exc)
            return False

        heartbeats = HeartbeatRunner(
            self.client,
            session_id=self.session_id,
            claim_id=claim["id"],
            interval_seconds=self.heartbeat_interval,
        )
        heartbeats.start()
        run: dict[str, Any] | None = None
        try:
            run = await self.client.start_run(
                task["id"], claim_id=claim["id"], fencing_token=claim["fencingToken"]
            )
            # The run id brings the run's instructions block along (CP-ADR-0066).
            context = await self.client.get_working_context(
                task_ref=task["id"], run_id=run["id"], project_id=self.project_id
            )
            # The thread of the task, as every adapter's prompt shows it.
            if self._own_principal is None:
                self._own_principal = await own_principal_id(self.client)
            task = await with_comments(self.client, task, own_principal=self._own_principal)
            env = await self._run_environment(task, run)
            # The server of a run stops with the run: its environment with it.
            async with self._opencode_for(env) as opencode:
                opencode_session, resumed = await self._resume_opencode_session(
                    opencode, run["id"], title=task.get("publicId") or task["title"]
                )
                await self.client.create_checkpoint(
                    run["id"],
                    kind=CHECKPOINT_KIND,
                    data={"openCodeSessionId": opencode_session, "resumed": resumed},
                )
                action = await self.client.record_action(
                    run["id"],
                    action="opencode.prompt",
                    status="started",
                    external_reference=f"opencode:session/{opencode_session}",
                )

                supervisor = RunSupervisor(self.client, run["id"], self.supervision)
                try:
                    reply = await supervisor.run(
                        opencode.send_prompt(
                            opencode_session,
                            self._build_prompt(task, context),
                            system=SYSTEM_PROMPT,
                            model=self.model,
                            agent=self.agent,
                        )
                    )
                except ExecutionStopped as stop:
                    # The request was dropped; the session itself is told to stop.
                    with contextlib.suppress(OpenCodeError):
                        await opencode.abort(opencode_session)
                    with contextlib.suppress(ControlPlaneError):
                        await self.client.finish_action(run["id"], action["id"], status="failed")
                    logger.warning("stopped %s: %s", task.get("publicId"), stop.reason)
                    await settle_stopped(
                        self.client,
                        run_id=run["id"],
                        claim_id=claim["id"],
                        fencing_token=int(claim["fencingToken"]),
                        stop=stop,
                    )
                    return True
            summary = reply_text(reply)
            message_id = reply_message_id(reply)
            await self.client.finish_action(
                run["id"],
                action["id"],
                status="completed",
                external_reference=(
                    f"opencode:session/{opencode_session}/message/{message_id}"
                    if message_id
                    else f"opencode:session/{opencode_session}"
                ),
            )
            await self.client.create_checkpoint(
                run["id"],
                kind=CHECKPOINT_KIND,
                data={
                    "openCodeSessionId": opencode_session,
                    "lastMessageId": message_id,
                },
            )
            if heartbeats.error is not None:
                await self.client.fail_run(run["id"], failure_reason="lease_lost")
                return False
            if summary:
                await self.client.create_artifact(
                    type="report",
                    name="opencode-summary",
                    task_ref=task["id"],
                    run_id=run["id"],
                    content={"summary": summary[:60_000]},
                )
            await self.client.succeed_run(run["id"], output={"messageId": message_id})
            return True
        except ServicesBlocked as exc:
            # Nothing ran: waiting changes nothing, a person moves the task.
            assert run is not None  # raised by the environment of a started run
            await settle_blocked(self.client, task, run, claim, exc.reason, failure_reason=exc.code)
            return True
        except StaleClaimError:
            if run is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(run["id"], failure_reason="ownership_lost")
            return False
        except OpenCodeError as exc:
            if run is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(run["id"], failure_reason=f"opencode: {exc}"[:500])
            logger.warning("opencode execution failed: %s", exc)
            return False
        except Exception as exc:
            if run is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.fail_run(
                        run["id"], failure_reason=f"{type(exc).__name__}: {exc}"[:500]
                    )
            raise
        finally:
            await heartbeats.stop()

    async def run_forever(self) -> None:
        await self.recover()
        await self._open_session()
        cycles = 0
        try:
            while not self._stop.is_set():
                if self.max_cycles is not None and cycles >= self.max_cycles:
                    return
                cycles += 1
                try:
                    worked = await self.run_once()
                except ControlPlaneError as exc:
                    logger.warning("cycle failed: %s", exc)
                    worked = False
                if not worked:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), self.poll_interval)
        finally:
            if self.session_heartbeats is not None:
                await self.session_heartbeats.stop()
            if self.session_id is not None:
                with contextlib.suppress(ControlPlaneError):
                    await self.client.close_session(self.session_id)


def main() -> int:  # pragma: no cover - process entrypoint
    from control_plane.logging import configure_logging

    configure_logging(os.environ.get("CP_LOG_LEVEL", "INFO"))
    stale = removed_environment_variables()
    for name, replacement in sorted(stale.items()):
        print(
            f"control-plane-opencode: {name} is no longer read (removed in v0.5); "
            f"use {replacement}",
            file=sys.stderr,
        )
    if stale:
        return 2

    server = os.environ.get("CONTROL_PLANE_SERVER", "").rstrip("/")
    if not server:
        print("control-plane-opencode: CONTROL_PLANE_SERVER is required", file=sys.stderr)
        return 2
    api_key = os.environ.get("CONTROL_PLANE_API_KEY") or resolve_api_key(server)
    if not api_key:
        print("control-plane-opencode: no credentials for the server", file=sys.stderr)
        return 2

    try:
        host_services = services_mode(os.environ) == "host"
    except ValueError as exc:
        print(f"control-plane-opencode: {exc}", file=sys.stderr)
        return 2
    workdir = Path.cwd()

    binary = os.environ.get("CONTROL_PLANE_OPENCODE_BINARY") or None
    # With the binary every run with an environment gets its own `opencode serve`
    # (server.py); the rest use the shared one.
    run_server = (
        OpenCodeServer(binary, password=os.environ.get("OPENCODE_SERVER_PASSWORD") or None)
        if binary
        else None
    )

    async def run() -> None:
        async with (
            ControlPlaneClient(server, api_key) as client,
            OpenCodeClient(
                os.environ.get("OPENCODE_SERVER", "http://127.0.0.1:4096"),
                password=os.environ.get("OPENCODE_SERVER_PASSWORD") or None,
            ) as opencode,
        ):
            adapter = OpenCodeAdapter(
                client,
                opencode,
                workspace_id=os.environ.get("CONTROL_PLANE_AGENT_WORKSPACE") or None,
                project_id=os.environ.get("CONTROL_PLANE_AGENT_PROJECT") or None,
                include_subprojects=os.environ.get("CONTROL_PLANE_AGENT_SUBPROJECTS") == "1",
                poll_interval=float(os.environ.get("CONTROL_PLANE_AGENT_POLL", "5")),
                model=os.environ.get("OPENCODE_MODEL") or None,
                agent=os.environ.get("OPENCODE_AGENT") or None,
                prompt_file=prompt_file_from_environment("CONTROL_PLANE_OPENCODE_PROMPT_FILE"),
                supervision=SupervisionSettings.from_environment(),
                server=run_server,
                # The repository OpenCode works in, at its checked-out commit:
                # the only runner.yaml this harness can read.
                declared_services=(
                    None if host_services else lambda: declared_services_at(workdir, "HEAD")
                ),
            )
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, adapter.request_stop)
            await adapter.run_forever()

    asyncio.run(run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
