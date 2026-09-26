"""OpenCode harness adapter (ADR-0041).

Speaks the Harness Protocol to the Control Plane through the official SDK and
drives an ``opencode serve`` process over its documented HTTP API. It is a
CLIENT: no database access, no core changes, and nothing product-specific
crosses into the Control Plane — ``harness.type = "opencode"`` is a value of an
existing column and ``skills.protocol.opencode`` already exists.

Continuity is server-side: the OpenCode session id and the last message id are
written to a Run Checkpoint, so after a restart the adapter resumes the SAME
OpenCode session instead of starting a fresh one.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from control_plane_agent.instructions import (
    build_prompt,
    prompt_file_from_environment,
    read_conventions,
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


class OpenCodeAdapter:
    # Repository conventions, the fourth instructions layer (CP-ADR-0066).
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
        # A cancel request or a run without progress stops the prompt
        # (control_plane_agent.supervision), as for every other adapter.
        self.supervision = supervision or SupervisionSettings()
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

    async def _resume_opencode_session(self, run_id: str, title: str) -> tuple[str, bool]:
        """Reuse the OpenCode session recorded on this run, else create one."""
        try:
            checkpoints = await self.client.list_checkpoints(run_id)
        except ControlPlaneError:
            checkpoints = {"items": []}
        for checkpoint in reversed(checkpoints.get("items", [])):
            if checkpoint.get("kind") != CHECKPOINT_KIND:
                continue
            candidate = (checkpoint.get("data") or {}).get("openCodeSessionId")
            if isinstance(candidate, str) and await self.opencode.session_exists(candidate):
                return candidate, True
        return await self.opencode.create_session(title=title), False

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
            opencode_session, resumed = await self._resume_opencode_session(
                run["id"], title=task.get("publicId") or task["title"]
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
                    self.opencode.send_prompt(
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
                    await self.opencode.abort(opencode_session)
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
