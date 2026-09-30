"""Claude Code harness adapter (ADR-0016 §1, AR-3).

Plugs into the reference daemon's ``Adapter`` contract: the daemon owns the
cycle — discovery, claim, run, workspace, commit evidence, completion — and this
adapter owns one question inside it, "make Claude Code do the work". Nothing
here reaches the database, and nothing vendor-specific crosses into the core:
``harness.type = "claude-code"`` is a value of an existing column.

**Continuity is server-side.** The Claude Code session id is written to a Run
Checkpoint before the CLI is started, so a runner that dies mid-task resumes the
same conversation on the next attempt instead of paying for a fresh one that has
forgotten what it already did.

**The agent inside talks to the Control Plane itself.** The MCP server is passed
through (Q4, decided 2026-08-14), so the agent reads its own run context and
leaves its own checkpoints rather than being told a second-hand summary in a
prompt. What it may not do is decide its own outcome: lifecycle commands stay
with this process, which is the one holding the claim and its fencing token.

**What the agent did is published, bounded (ADR-0051).** Besides the final
summary, the adapter publishes a ``transcript`` artifact — assistant messages,
tool calls with their inputs and results, the final answer — redacted of host
paths and credentials and capped in size, and records one run action per tool
call while the turn is still running. The prompt and hidden reasoning
(``thinking`` blocks) never leave the host; the raw stream stays in the local
log. ``CONTROL_PLANE_TRACE_*`` narrows this per deployment.
"""

from __future__ import annotations

import logging
import os
import uuid
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
from control_plane_agent.trace import (
    TraceRecorder,
    TraceSettings,
    TranscriptBuilder,
    text_of_blocks,
)
from control_plane_agent.workspace import Workspace, assert_portable, redact_local_paths
from control_plane_claude.cli import (
    ClaudeCodeCLI,
    ClaudeCodeError,
    ClaudeResult,
    withheld_tools,
    write_mcp_config,
)
from control_plane_client import ControlPlaneClient, ControlPlaneError, is_transient
from control_plane_mcp.environment import MCP_RUN_ENV, MCP_TASK_ENV

logger = logging.getLogger("control_plane_claude")

HARNESS_TYPE = "claude-code"
CHECKPOINT_KIND = "claude-code.session"
MAX_SUMMARY_CHARS = 60_000

# What only this harness can say about its environment. The protocol, the
# report and the bounds of authority come from the Control Plane as the
# platform contract layer of the instructions (CP-ADR-0066).
SYSTEM_NOTE = """\
You are running under the Claude Code adapter of a Control Plane runner.

The `control-plane` MCP server is available: use it to read the run context,
record what you did, and leave checkpoints. The lifecycle commands are withheld:
the runner that started you owns the lease and decides the outcome.

Work only inside the current directory: it is an isolated copy checked out on
this task's branch. Do not push, and do not open a pull request: the runner
commits your work and publishes the branch itself, so a push from here would
only race it. Leave the code in a state worth committing and worth reviewing —
the branch goes to people, not into a void.

Your final message is published as the run's summary; your messages and tool
calls are recorded as a bounded transcript for audit, your hidden reasoning is
not.

A file that is a result of this task and not part of the code — a document, a
report — is handed in with `cp_create_artifact(type=..., name=..., file=...)`:
the file is uploaded to the Control Plane and becomes an artifact of this run.

If you cannot do the work, leave `cp_checkpoint(kind="blocked", data={"reason":
"<why>"})` before you finish: the runner then fails the run and hands the task
to a person instead of publishing it as done. Nothing is committed in that case,
and the working copy stays as you left it.
"""


@dataclass
class ClaudeCodeAdapter:
    """Executes one task by driving Claude Code inside the task's working copy."""

    cli: ClaudeCodeCLI
    resume_sessions: bool = True
    include_memory: bool = True
    withhold_authoritative_tools: bool = True
    trace: TraceSettings = field(default_factory=TraceSettings)
    # Repository conventions appended to every prompt (naming, registries,
    # what never goes into a commit, how to run tests). Read at execute time
    # so an operator can edit the file without restarting the runner. A task
    # description says WHAT; this file says HOW this repository works — the
    # kind of knowledge that otherwise costs a review round per branch.
    prompt_file: Path | None = None
    # Executor instructions of the agent's revision (CP-ADR-0073): the same
    # fourth layer, given as text instead of a file. When set, the file is
    # not read.
    instructions: str | None = None
    _tools_narrowed: bool = field(default=False, init=False, repr=False)

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

        session_id, resume = await self._session_for(client, run_id)
        # Checkpoint BEFORE the process starts: an id known only to a process
        # that then dies is an id nobody can resume.
        await self._checkpoint(
            client, run_id, {"claudeSessionId": session_id, "resumed": resume, "phase": "started"}
        )

        await self._narrow_tools()
        prompt = self._build_prompt(task, await self._context(client, task, run_id), inputs)
        action = await client.record_action(
            run_id,
            action="claude-code.turn",
            status="started",
            external_reference=f"claude-code:session/{session_id}",
        )
        builder = TranscriptBuilder(
            HARNESS_TYPE, session_id=session_id, keep_tool_results=self.trace.tool_results
        )
        recorder = TraceRecorder(
            client,
            run_id,
            builder=builder,
            settings=self.trace,
            reference_prefix=f"claude-code:session/{session_id}",
            logger=logger,
        )
        try:
            result = await self.cli.run_turn(
                prompt,
                cwd=cwd,
                session_id=session_id,
                resume=resume,
                # The MCP server the agent starts inherits these: what it
                # records — artifacts, checkpoints, actions — belongs to this
                # run. Ids, not credentials; the lease stays with the daemon.
                env={MCP_TASK_ENV: str(task["id"]), MCP_RUN_ENV: run_id},
                log_name=public_id,
                on_event=lambda event: consume_stream_event(recorder, event),
            )
        except ClaudeCodeError as exc:
            await recorder.close(failed=True)
            await with_suppressed(client.finish_action(run_id, str(action["id"]), status="failed"))
            await self._checkpoint(
                client,
                run_id,
                # The message may name a local path or a binary; the checkpoint
                # is read elsewhere, so only the shape of the failure travels.
                # The detail stays in the runner's log.
                {
                    "claudeSessionId": session_id,
                    "phase": "failed",
                    "errorType": type(exc).__name__,
                },
            )
            raise

        # Bookkeeping of a turn that has already run: a core unreachable for
        # the moment must not turn finished work into a failed run.
        await with_suppressed(
            client.finish_action(
                run_id,
                str(action["id"]),
                status="completed" if not result.is_error else "failed",
                external_reference=f"claude-code:session/{result.session_id}",
            ),
            only_transient=True,
        )
        await self._checkpoint(
            client,
            run_id,
            {
                "claudeSessionId": result.session_id,
                "phase": "finished",
                "subtype": result.subtype,
                "turns": result.turns,
            },
        )
        await recorder.close(failed=result.is_error)
        if result.is_error:
            # An error subtype is a real failure of the turn: let the daemon fail
            # the run honestly rather than publish a half-done result as success.
            raise ClaudeCodeError(f"claude turn ended as {result.subtype or 'error'}")
        builder.session_id = result.session_id
        builder.record_usage(
            inputTokens=result.input_tokens,
            outputTokens=result.output_tokens,
            costUsd=result.cost_usd,
            durationMs=result.duration_ms,
            turns=result.turns,
        )
        artifacts = [self._summary_artifact(public_id, result)]
        transcript = recorder.artifact(
            name=f"claude-code transcript for {public_id}",
            extra_metadata={"claudeSessionId": result.session_id, "turns": result.turns},
        )
        if transcript is not None:
            artifacts.append(transcript)
        return artifacts

    # -- pieces ----------------------------------------------------------------

    async def _narrow_tools(self) -> None:
        """Withhold the authoritative commands from the agent inside.

        Resolved once per process and only when the MCP server is actually
        passed through: without it there is no Control Plane tool to withhold.
        The tools a revision allows or denies (``tools.allow/deny``) are
        applied on top: a deny is added to the withheld set, and an allow
        never names a withheld command — the CLI lets a deny win anyway, but
        the argv should not say otherwise.
        """
        if self._tools_narrowed:
            return
        self._tools_narrowed = True
        if not self.withhold_authoritative_tools or self.cli.mcp_config_path is None:
            return
        withheld = await withheld_tools()
        self.cli.disallowed_tools = list(dict.fromkeys([*withheld, *self.cli.disallowed_tools]))
        self.cli.allowed_tools = [t for t in self.cli.allowed_tools if t not in frozenset(withheld)]

    async def _session_for(self, client: ControlPlaneClient, run_id: str) -> tuple[str, bool]:
        """Resume the session recorded on this run, or name a new one."""
        if self.resume_sessions:
            try:
                checkpoints = await client.list_checkpoints(run_id)
            except ControlPlaneError:
                checkpoints = {"items": []}
            for checkpoint in reversed(list(checkpoints.get("items", []))):
                if checkpoint.get("kind") != CHECKPOINT_KIND:
                    continue
                candidate = (checkpoint.get("data") or {}).get("claudeSessionId")
                if isinstance(candidate, str) and candidate:
                    return candidate, True
        return str(uuid.uuid4()), False

    async def _context(
        self, client: ControlPlaneClient, task: dict[str, Any], run_id: str
    ) -> dict[str, Any]:
        try:
            context = await client.get_working_context(
                task_ref=str(task["id"]), run_id=run_id, include_memory=self.include_memory
            )
        except ControlPlaneError as exc:
            # Context is an aid, not a precondition: the task itself is
            # authoritative, and the agent can read the rest over MCP.
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

    def _summary_artifact(self, public_id: str, result: ClaudeResult) -> ArtifactSpec:
        # The summary is free text written by a model that has just been working
        # in a directory, so it names paths as a matter of course — and a path of
        # this host must not reach durable state. Redact at the source: the
        # daemon's assert_portable is a last line of defence, and a last line
        # that trips on ordinary output is a broken run, not a caught mistake.
        # Learned the hard way on TASK-000040: half an hour of finished work was
        # failed and its report lost because the agent mentioned a local path.
        summary = redact_local_paths(result.summary)[:MAX_SUMMARY_CHARS]
        content: dict[str, Any] = {"summary": summary}
        metadata: dict[str, Any] = {
            "harnessType": HARNESS_TYPE,
            "claudeSessionId": result.session_id,
            "turns": result.turns,
            "durationMs": result.duration_ms,
        }
        if result.model:
            metadata["model"] = result.model
        if result.cost_usd is not None:
            metadata["costUsd"] = result.cost_usd
        if result.input_tokens is not None:
            metadata["inputTokens"] = result.input_tokens
        if result.output_tokens is not None:
            metadata["outputTokens"] = result.output_tokens
        return ArtifactSpec(
            type="report",
            name=f"claude-code summary for {public_id}",
            content=content,
            metadata=metadata,
        )


async def consume_stream_event(recorder: TraceRecorder, event: dict[str, Any]) -> None:
    """Map one stream-json event of ``claude -p`` onto the trace.

    Shapes (observed on the runner, 2026-09): ``assistant`` events carry one
    content block each — ``text``, ``tool_use`` or ``thinking``; ``user``
    events carry ``tool_result`` blocks (or plain text); ``system/init`` names
    the model and the tools; ``result`` carries the final text and usage.
    Anything else (rate-limit notices, thinking-token counters) is noise here.
    """
    etype = event.get("type")
    at = event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
    if etype == "system" and event.get("subtype") == "init":
        tools = event.get("tools")
        recorder.builder.system(
            model=str(event.get("model") or ""),
            tools=[str(t) for t in tools] if isinstance(tools, list) else None,
        )
        return
    raw_message = event.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    content = message.get("content")
    if etype == "assistant":
        if isinstance(content, str):
            recorder.builder.assistant_text(content, at=at)
            return
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                recorder.builder.assistant_text(block["text"], at=at)
            elif kind == "tool_use":
                await recorder.tool_started(
                    str(block.get("id") or ""),
                    str(block.get("name") or "tool"),
                    block.get("input"),
                    at=at,
                )
            elif kind in ("thinking", "redacted_thinking"):
                recorder.builder.hidden_reasoning()
        return
    if etype == "user":
        if isinstance(content, str):
            recorder.builder.user_text(content, at=at)
            return
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                await recorder.tool_finished(
                    str(block.get("tool_use_id") or ""),
                    text_of_blocks(block.get("content")),
                    is_error=bool(block.get("is_error")),
                    at=at,
                )
            elif kind == "text" and isinstance(block.get("text"), str):
                recorder.builder.user_text(block["text"], at=at)
        return
    if etype == "result":
        text = event.get("result")
        if isinstance(text, str):
            recorder.builder.final_answer(text)


async def with_suppressed(awaitable: Awaitable[Any], *, only_transient: bool = False) -> None:
    """Await an auxiliary write, tolerating its failure.

    Checkpoints and action bookkeeping must never turn a finished piece of work
    into a failure; the authoritative outcome is decided by the daemon. It is
    still awaited rather than fired and forgotten — a checkpoint that lands
    after the process it describes has died records nothing useful.

    ``only_transient`` tolerates only an unreachable core (:func:`is_transient`):
    an answer of the core itself — a lost claim, a finished run — still stops
    the execution.
    """
    try:
        await awaitable
    except ControlPlaneError as exc:
        if only_transient and not is_transient(exc):
            raise
        logger.info("auxiliary write failed: %s", exc)


def _host_cli(environ: Mapping[str, str]) -> ClaudeCodeCLI:
    """What belongs to the host, not to the agent: binary, MCP, local logs."""
    runtime_dir = Path(
        environ.get("CONTROL_PLANE_CLAUDE_RUNTIME_DIR") or Path.home() / ".claude-runner"
    )
    mcp_config: Path | None = None
    if environ.get("CONTROL_PLANE_CLAUDE_MCP", "1") == "1":
        mcp_config = write_mcp_config(runtime_dir / "mcp.json")
    log_dir: Path | None = None
    if environ.get("CONTROL_PLANE_CLAUDE_LOGS", "1") == "1":
        log_dir = runtime_dir / "sessions"
    return ClaudeCodeCLI(
        binary=environ.get("CONTROL_PLANE_CLAUDE_BINARY", "claude"),
        mcp_config_path=mcp_config,
        log_dir=log_dir,
    )


def adapter_from_environment() -> ClaudeCodeAdapter:
    """Build the adapter the daemon will use, from the runner's environment."""
    cli = _host_cli(os.environ)
    cli.model = os.environ.get("CONTROL_PLANE_CLAUDE_MODEL") or None
    cli.permission_mode = os.environ.get("CONTROL_PLANE_CLAUDE_PERMISSION_MODE", "acceptEdits")
    cli.timeout_seconds = float(os.environ.get("CONTROL_PLANE_CLAUDE_TIMEOUT", "3600"))
    return ClaudeCodeAdapter(
        cli,
        resume_sessions=os.environ.get("CONTROL_PLANE_CLAUDE_RESUME", "1") == "1",
        trace=TraceSettings.from_environment(),
        prompt_file=prompt_file_from_environment("CONTROL_PLANE_CLAUDE_PROMPT_FILE"),
    )


#: ``executor.params`` of kind ``claude-code`` (``$defs.agentExecutors``).
PARAMS = frozenset({"model", "permissionMode", "timeoutSeconds", "resume", "tools"})
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")


def adapter_from_params(
    params: Mapping[str, Any],
    *,
    instructions: str = "",
    environ: Mapping[str, str] | None = None,
) -> ClaudeCodeAdapter:
    """Build the adapter from the parameters of an agent revision (CP-ADR-0073).

    The revision says how the agent works — model, permission mode, turn
    timeout, resumption, which tools it may use — and ``instructions`` replace
    the conventions file. The host keeps what is its own: the binary, the MCP
    passthrough, local logs and the trace switches. The core stores the
    parameters without reading them, so they are checked here: an unknown
    or malformed one is a ``ValueError``, not a default nobody asked for.
    """
    environ = os.environ if environ is None else environ
    unknown = sorted(set(params) - PARAMS)
    if unknown:
        raise ValueError(f"claude-code: unknown executor params {unknown}")
    mode = params.get("permissionMode", "acceptEdits")
    if mode not in PERMISSION_MODES:
        raise ValueError(f"claude-code: permissionMode {mode!r} is not one of {PERMISSION_MODES}")
    model = params.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError("claude-code: model must be a non-empty string")
    timeout = params.get("timeoutSeconds", 3600)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("claude-code: timeoutSeconds must be a positive integer")
    resume = params.get("resume", True)
    if not isinstance(resume, bool):
        raise ValueError("claude-code: resume must be a boolean")
    tools = params.get("tools") or {}
    if not isinstance(tools, Mapping) or set(tools) - {"allow", "deny"}:
        raise ValueError("claude-code: tools takes only allow and deny")
    allow, deny = (_tool_names(tools.get(side), side) for side in ("allow", "deny"))
    cli = _host_cli(environ)
    cli.model = model
    cli.permission_mode = mode
    cli.timeout_seconds = float(timeout)
    cli.allowed_tools = allow
    cli.disallowed_tools = deny
    return ClaudeCodeAdapter(
        cli,
        resume_sessions=resume,
        trace=TraceSettings.from_environment(environ),
        instructions=instructions,
    )


def _tool_names(value: Any, side: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) and t for t in value):
        raise ValueError(f"claude-code: tools.{side} must be a list of tool names")
    return list(dict.fromkeys(value))
