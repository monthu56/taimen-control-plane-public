"""Codex harness adapter (AR-8).

Plugs into the reference daemon's ``Adapter`` contract — the same one
``control_plane_claude`` implements: the daemon owns the cycle (discovery,
claim, run, workspace, commit evidence, completion) and this adapter owns one
question inside it, "make Codex do the work". Nothing here reaches the
database, and nothing vendor-specific crosses into the core:
``harness.type = "codex"`` is a value of an existing column, same as
``"claude-code"``.

**Continuity is server-side, with one honest gap.** Claude Code lets the
caller pick a session id before the process starts, so the checkpoint always
precedes the turn. Codex does not: a fresh ``codex exec`` assigns its own
thread id and reports it as the first line of output, so for a brand new
session the id can only be checkpointed the instant it is read off the
stream — not before the process starts. See ``cli.py`` for the mechanics
(the ``on_thread_id`` callback) and why this is a property of the vendor CLI,
not a shortcut taken here. Resuming an already-known session does not have
this gap: the id is checkpointed before the process starts, exactly like
Claude Code.

**No MCP passthrough.** ``control_plane_claude`` hands the agent inside the
Control Plane MCP server so it can read its own run context. That decision is
Claude-Code-specific and out of scope here (not requested by AR-8, and codex
exec's non-interactive MCP wiring goes through a shared ``config.toml`` rather
than a per-invocation flag, which would risk clobbering a host's existing
Codex configuration). Instead, same as ``control_plane_opencode``, the working
context is embedded directly in the prompt.

**Nothing said in the conversation becomes an artifact.** The transcript stays
on the runner (rotated local log); what is published is the final summary and
counters — no prompts, no reasoning, no tool traffic (harness-protocol §7, §18).

**auth.json is not this module's concern, deliberately.** Codex updates
``$CODEX_HOME/auth.json`` (default ``~/.codex/auth.json``) in place on every
run, whether the credential is a ChatGPT subscription login or an API key.
This adapter never sets, reads, or clears ``CODEX_HOME`` — the subprocess
simply inherits the runner's environment (see ``cli.py``), the same way it
inherits ``OPENAI_API_KEY``. Whatever directory ``CODEX_HOME`` names must be a
volume that survives a container restart, or a fresh login is required after
every one; that is a deployment concern for whoever operates the runner, not
something this package can enforce from inside a subprocess call.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from control_plane_agent.inputs import LocalInput
from control_plane_agent.instructions import (
    build_prompt,
    prompt_file_from_environment,
    read_conventions,
)
from control_plane_agent.main import ArtifactSpec
from control_plane_agent.trace import TraceRecorder, TraceSettings, TranscriptBuilder
from control_plane_agent.workspace import Workspace, assert_portable, redact_local_paths
from control_plane_client import ControlPlaneClient, ControlPlaneError
from control_plane_codex.cli import (
    CodexCLI,
    CodexError,
    CodexQuotaExhaustedError,
    CodexResult,
    is_quota_exhausted,
)

logger = logging.getLogger("control_plane_codex")

HARNESS_TYPE = "codex"
CHECKPOINT_KIND = "codex.session"
MAX_SUMMARY_CHARS = 60_000

# What only this harness can say about its environment. The protocol, the
# report and the bounds of authority come from the Control Plane as the
# platform contract layer of the instructions (CP-ADR-0066).
SYSTEM_NOTE = """\
You are running under the Codex adapter of a Control Plane runner.

The task and its authoritative context are given below in this prompt — there
is no Control Plane tool available to you in this session, so treat what
follows as the full picture. The runner that started you owns the lease and
decides the outcome.

Work only inside the current directory: it is an isolated copy checked out on
this task's branch. Do not push, and do not open a pull request: the runner
commits your work and publishes the branch itself, so a push from here would
only race it. Leave the code in a state worth committing and worth reviewing —
the branch goes to people, not into a void.

Your final message is published as the run's summary; your messages, commands
and tool calls are recorded as a bounded transcript for audit, your reasoning
is not.
"""


@dataclass
class CodexAdapter:
    """Executes one task by driving Codex inside the task's working copy."""

    cli: CodexCLI
    resume_sessions: bool = True
    include_memory: bool = True
    # AR-4's credential pool does not exist yet; this only names, for a given
    # runner, which credential family it was configured with (subscription
    # login vs. API key), so it can travel on artifacts/checkpoints ahead of
    # the pool that will one day choose it automatically.
    credential_class: str | None = None
    trace: TraceSettings = field(default_factory=TraceSettings)
    # Repository conventions, the fourth instructions layer (CP-ADR-0066):
    # read at execute time, like the Claude Code adapter's file.
    prompt_file: Path | None = None
    # Executor instructions of the agent's revision (CP-ADR-0073), instead of
    # the file when set.
    instructions: str | None = None

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        client: ControlPlaneClient,
        workspace: Workspace | None,
        inputs: Sequence[LocalInput] | None = None,
    ) -> list[ArtifactSpec]:
        run_id = str(run["id"])
        public_id = str(task.get("publicId") or task["id"])
        cwd = workspace.path if workspace is not None else Path.cwd()

        resume_id = await self._resume_target(client, run_id)
        if resume_id is not None:
            # Known before the process starts: the id came from an earlier
            # checkpoint, so a resumed run is continuous from turn one.
            await self._checkpoint(
                client,
                run_id,
                {"codexSessionId": resume_id, "resumed": True, "phase": "started"},
            )

        async def on_thread_id(thread_id: str) -> None:
            # For a fresh session this is the earliest the id can possibly be
            # known — see the module docstring for why it cannot be earlier.
            await self._checkpoint(
                client,
                run_id,
                {
                    "codexSessionId": thread_id,
                    "resumed": resume_id is not None,
                    "phase": "started",
                },
            )

        prompt = self._build_prompt(task, await self._context(client, task, run_id), inputs)
        action = await client.record_action(
            run_id,
            action="codex.turn",
            status="started",
            external_reference=f"codex:session/{resume_id or 'pending'}",
        )
        builder = TranscriptBuilder(
            HARNESS_TYPE, session_id=resume_id or "", keep_tool_results=self.trace.tool_results
        )
        recorder = TraceRecorder(
            client,
            run_id,
            builder=builder,
            settings=self.trace,
            reference_prefix=f"codex:session/{resume_id or 'pending'}",
            logger=logger,
        )
        mapper = CodexEventMapper(recorder)
        try:
            result = await self.cli.run_turn(
                prompt,
                cwd=cwd,
                resume_session_id=resume_id,
                log_name=public_id,
                on_thread_id=on_thread_id,
                on_event=mapper.consume,
            )
        except CodexError as exc:
            await recorder.close(failed=True)
            await with_suppressed(client.finish_action(run_id, str(action["id"]), status="failed"))
            await self._checkpoint(
                client,
                run_id,
                # The message may name a local path or a binary; the checkpoint
                # is read elsewhere, so only the shape of the failure travels.
                # The detail stays in the runner's log.
                {"phase": "failed", "errorType": type(exc).__name__},
            )
            raise

        await client.finish_action(
            run_id,
            str(action["id"]),
            status="completed" if not result.is_error else "failed",
            external_reference=f"codex:session/{result.session_id}",
        )
        await self._checkpoint(
            client,
            run_id,
            {
                "codexSessionId": result.session_id,
                "phase": "finished",
                "durationMs": result.duration_ms,
            },
        )
        await recorder.close(failed=result.is_error)
        if result.is_error:
            # An error turn is a real failure: let the daemon fail the run
            # honestly rather than publish a half-done result as success.
            error_cls = (
                CodexQuotaExhaustedError if is_quota_exhausted(result.error_message) else CodexError
            )
            raise error_cls(f"codex turn ended in error: {result.error_message or 'unknown'}")
        builder.session_id = result.session_id
        builder.final_answer(result.summary)
        builder.record_usage(
            inputTokens=result.input_tokens,
            outputTokens=result.output_tokens,
            durationMs=result.duration_ms,
        )
        artifacts = [self._summary_artifact(public_id, result)]
        transcript = recorder.artifact(
            name=f"codex transcript for {public_id}",
            extra_metadata={"codexSessionId": result.session_id},
        )
        if transcript is not None:
            artifacts.append(transcript)
        return artifacts

    # -- pieces ----------------------------------------------------------------

    async def _resume_target(self, client: ControlPlaneClient, run_id: str) -> str | None:
        """The session id recorded on this run, if any and if resuming is on.

        Unlike Claude Code there is no "else generate a new one" branch here:
        Codex assigns a fresh session's id itself, so None simply means start
        one and let ``on_thread_id`` learn what it turned out to be.
        """
        if not self.resume_sessions:
            return None
        try:
            checkpoints = await client.list_checkpoints(run_id)
        except ControlPlaneError:
            checkpoints = {"items": []}
        for checkpoint in reversed(list(checkpoints.get("items", []))):
            if checkpoint.get("kind") != CHECKPOINT_KIND:
                continue
            candidate = (checkpoint.get("data") or {}).get("codexSessionId")
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    async def _context(
        self, client: ControlPlaneClient, task: dict[str, Any], run_id: str
    ) -> dict[str, Any]:
        try:
            context = await client.get_working_context(
                task_ref=str(task["id"]), run_id=run_id, include_memory=self.include_memory
            )
        except ControlPlaneError as exc:
            # Context is an aid, not a precondition: the task itself is
            # authoritative, and it is embedded in the prompt below regardless.
            logger.info("working context unavailable: %s", exc)
            return {}
        return context if isinstance(context, dict) else {}

    def _build_prompt(
        self,
        task: dict[str, Any],
        context: dict[str, Any],
        inputs: Sequence[LocalInput] | None = None,
    ) -> str:
        return build_prompt(
            task,
            context,
            harness_note=SYSTEM_NOTE,
            conventions=(
                self.instructions.strip()
                if self.instructions is not None
                else read_conventions(self.prompt_file)
            ),
            inputs=inputs,
        )

    async def _checkpoint(
        self, client: ControlPlaneClient, run_id: str, data: dict[str, Any]
    ) -> None:
        # Same rule as artifacts: a checkpoint is read by other principals in
        # other environments, so no local path and no credential may be in it.
        assert_portable(data, where="checkpoint")
        await with_suppressed(client.create_checkpoint(run_id, kind=CHECKPOINT_KIND, data=data))

    def _summary_artifact(self, public_id: str, result: CodexResult) -> ArtifactSpec:
        # Same reason as in the Claude Code adapter: the summary is free text
        # from a model that has been working in a directory, and a path of this
        # host must not reach durable state. Redact at the source rather than
        # let the daemon's last-line guard fail an otherwise finished run.
        summary = redact_local_paths(result.summary)[:MAX_SUMMARY_CHARS]
        content: dict[str, Any] = {"summary": summary}
        metadata: dict[str, Any] = {
            "harnessType": HARNESS_TYPE,
            "codexSessionId": result.session_id,
            "durationMs": result.duration_ms,
        }
        if result.input_tokens is not None:
            metadata["inputTokens"] = result.input_tokens
        if result.output_tokens is not None:
            metadata["outputTokens"] = result.output_tokens
        if self.credential_class:
            metadata["credentialClass"] = self.credential_class
        return ArtifactSpec(
            type="report",
            name=f"codex summary for {public_id}",
            content=content,
            metadata=metadata,
        )


class CodexEventMapper:
    """Maps the ``codex exec --json`` item stream onto the trace.

    Items arrive as ``item.started`` (for long-running ones: commands, MCP
    calls) and ``item.completed``. A command is a tool call named
    ``command_execution``; an MCP call is ``mcp.<server>.<tool>``; a file
    change and a web search are instantaneous calls. ``agent_message`` is
    assistant text (the last one is also the summary), ``reasoning`` is
    counted and not stored.
    """

    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder
        self._started: set[str] = set()

    async def consume(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "thread.started":
            candidate = event.get("thread_id")
            if isinstance(candidate, str) and candidate and not self.recorder.builder.session_id:
                self.recorder.builder.session_id = candidate
                self.recorder.reference_prefix = f"codex:session/{candidate}"
            return
        if etype not in ("item.started", "item.completed"):
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_id = str(item.get("id") or f"item-{id(item)}")
        kind = item.get("type")
        completed = etype == "item.completed"
        if kind == "agent_message":
            if completed and isinstance(item.get("text"), str):
                self.recorder.builder.assistant_text(item["text"])
            return
        if kind == "reasoning":
            if completed:
                self.recorder.builder.hidden_reasoning()
            return
        if kind == "command_execution":
            name = "command_execution"
            payload: dict[str, Any] = {"command": item.get("command")}
            output: Any = item.get("aggregated_output")
            exit_code = item.get("exit_code")
            failed = item.get("status") == "failed" or (
                isinstance(exit_code, int) and exit_code != 0
            )
            if exit_code is not None:
                output = f"[exit {exit_code}]\n{output or ''}"
        elif kind == "mcp_tool_call":
            name = f"mcp.{item.get('server', '')}.{item.get('tool', '')}"
            payload = {"arguments": item.get("arguments")}
            output = item.get("error") or item.get("result")
            failed = item.get("status") == "failed" or bool(item.get("error"))
        elif kind == "file_change":
            name = "file_change"
            payload = {"changes": item.get("changes")}
            output = str(item.get("status") or "completed")
            failed = item.get("status") == "failed"
        elif kind == "web_search":
            name = "web_search"
            payload = {"query": item.get("query")}
            output = str(item.get("status") or "completed")
            failed = False
        else:
            return
        if item_id not in self._started:
            self._started.add(item_id)
            await self.recorder.tool_started(item_id, name, payload)
        if completed:
            await self.recorder.tool_finished(item_id, output, is_error=failed)


async def with_suppressed(awaitable: Awaitable[Any]) -> None:
    """Await an auxiliary write, tolerating its failure.

    Checkpoints and action bookkeeping must never turn a finished piece of work
    into a failure; the authoritative outcome is decided by the daemon. It is
    still awaited rather than fired and forgotten — a checkpoint that lands
    after the process it describes has died records nothing useful.
    """
    try:
        await awaitable
    except ControlPlaneError as exc:
        logger.info("auxiliary write failed: %s", exc)


def _host_cli(environ: Mapping[str, str]) -> CodexCLI:
    """What belongs to the host, not to the agent: the binary and local logs."""
    log_dir: Path | None = None
    if environ.get("CONTROL_PLANE_CODEX_LOGS", "1") == "1":
        runtime_dir = Path(
            environ.get("CONTROL_PLANE_CODEX_RUNTIME_DIR") or Path.home() / ".codex-runner"
        )
        log_dir = runtime_dir / "sessions"
    return CodexCLI(binary=environ.get("CONTROL_PLANE_CODEX_BINARY", "codex"), log_dir=log_dir)


def adapter_from_environment() -> CodexAdapter:
    """Build the adapter the daemon will use, from the runner's environment."""
    cli = _host_cli(os.environ)
    cli.model = os.environ.get("CONTROL_PLANE_CODEX_MODEL") or None
    cli.sandbox = os.environ.get("CONTROL_PLANE_CODEX_SANDBOX", "workspace-write")
    cli.timeout_seconds = float(os.environ.get("CONTROL_PLANE_CODEX_TIMEOUT", "3600"))
    return CodexAdapter(
        cli,
        resume_sessions=os.environ.get("CONTROL_PLANE_CODEX_RESUME", "1") == "1",
        trace=TraceSettings.from_environment(),
        credential_class=os.environ.get("CONTROL_PLANE_CODEX_CREDENTIAL_CLASS") or None,
        prompt_file=prompt_file_from_environment("CONTROL_PLANE_CODEX_PROMPT_FILE"),
    )


#: ``executor.params`` of kind ``codex`` (``$defs.agentExecutors``).
PARAMS = frozenset({"model", "sandbox", "timeoutSeconds", "resume", "credentialClass"})
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CREDENTIAL_CLASSES = ("subscription", "api_key")


def adapter_from_params(
    params: Mapping[str, Any],
    *,
    instructions: str = "",
    environ: Mapping[str, str] | None = None,
) -> CodexAdapter:
    """Build the adapter from the parameters of an agent revision (CP-ADR-0073).

    Same split as the Claude Code adapter: the revision gives the model, the
    sandbox, the turn timeout, resumption and the credential class, and its
    ``instructions`` replace the conventions file; the host keeps the binary,
    local logs and the trace switches. Unknown or malformed parameters are a
    ``ValueError``.
    """
    environ = os.environ if environ is None else environ
    unknown = sorted(set(params) - PARAMS)
    if unknown:
        raise ValueError(f"codex: unknown executor params {unknown}")
    sandbox = params.get("sandbox", "workspace-write")
    if sandbox not in SANDBOXES:
        raise ValueError(f"codex: sandbox {sandbox!r} is not one of {SANDBOXES}")
    credential_class = params.get("credentialClass")
    if credential_class is not None and credential_class not in CREDENTIAL_CLASSES:
        raise ValueError(
            f"codex: credentialClass {credential_class!r} is not one of {CREDENTIAL_CLASSES}"
        )
    model = params.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError("codex: model must be a non-empty string")
    timeout = params.get("timeoutSeconds", 3600)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("codex: timeoutSeconds must be a positive integer")
    resume = params.get("resume", True)
    if not isinstance(resume, bool):
        raise ValueError("codex: resume must be a boolean")
    cli = _host_cli(environ)
    cli.model = model
    cli.sandbox = sandbox
    cli.timeout_seconds = float(timeout)
    return CodexAdapter(
        cli,
        resume_sessions=resume,
        trace=TraceSettings.from_environment(environ),
        credential_class=credential_class,
        instructions=instructions,
    )
