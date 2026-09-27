"""Driving the Claude Code CLI in non-interactive mode.

This module knows one thing: how to run ``claude -p`` over a working copy and
read back what happened. It holds no Control Plane concepts — the adapter above
it does — so the two can be reasoned about, and tested, apart.

Three choices here are deliberate and easy to get wrong.

The prompt goes in over **stdin**, never as an argument: an argument is visible
in the process table to everyone on the host. The same rule closes the door on
credentials — the OAuth token reaches the child through the environment it
inherits, and the MCP servers it starts inherit it in turn, so no secret is ever
written into argv or into a config file for this run.

The session id is **generated here and passed in**, rather than read out of the
reply. That is what makes continuity honest: the id is known before the process
starts, so it can be checkpointed even if the run dies mid-flight, and the next
attempt resumes the same conversation instead of starting a fresh one.

The raw stream stays **on this host**. Every line is appended to a local log
file, capped at a total size across the file's whole lifetime, including
turns resumed from an earlier session. What travels to the Control Plane is
decided one layer up: the adapter receives every parsed event through
``on_event`` and publishes a bounded, redacted transcript plus the structured
result (ADR-0051). Prompts and hidden reasoning never leave (harness-protocol
§7, §18).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BINARY = "claude"
DEFAULT_TIMEOUT_SECONDS = 3600.0
# A single line of stream-json is bounded in practice, but a runaway tool result
# should not be able to exhaust the runner's memory before the cap applies.
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 32 * 1024 * 1024
# stderr is not published anywhere; this is only how much of it we keep in
# memory to fold into the error message when a turn ends without a result.
STDERR_TAIL_BYTES = 8 * 1024

MCP_SERVER_NAME = "control-plane"


class ClaudeCodeError(RuntimeError):
    """The CLI could not be run, or did not finish a turn."""


@dataclass(frozen=True)
class ClaudeResult:
    """What one non-interactive turn produced.

    ``summary`` is the assistant's final text — the only free-form content that
    is allowed to leave this host, and even then as an artifact the adapter
    truncates.
    """

    session_id: str
    summary: str
    subtype: str
    is_error: bool
    turns: int
    duration_ms: int
    cost_usd: float | None = None
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


EventHook = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ClaudeCodeCLI:
    """Runs ``claude -p`` and parses its stream-json output."""

    binary: str = DEFAULT_BINARY
    model: str | None = None
    permission_mode: str = "acceptEdits"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    mcp_config_path: Path | None = None
    disallowed_tools: Sequence[str] = ()
    allowed_tools: Sequence[str] = ()
    log_dir: Path | None = None
    extra_args: Sequence[str] = field(default_factory=tuple)

    def command(self, *, session_id: str, resume: bool) -> list[str]:
        """Build argv. Nothing secret may appear here — see module docstring."""
        args = [
            self.binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            self.permission_mode,
        ]
        # --resume continues the recorded conversation; --session-id names a new
        # one. Passing both is what the CLI rejects, so they are exclusive here.
        args += ["--resume", session_id] if resume else ["--session-id", session_id]
        if self.model:
            args += ["--model", self.model]
        if self.mcp_config_path is not None:
            # strict: the agent gets exactly the servers we declare, not
            # whatever happens to be configured for the user running the runner.
            args += ["--mcp-config", str(self.mcp_config_path), "--strict-mcp-config"]
        if self.allowed_tools:
            args += ["--allowedTools", *self.allowed_tools]
        if self.disallowed_tools:
            args += ["--disallowedTools", *self.disallowed_tools]
        args += list(self.extra_args)
        return args

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        resume: bool = False,
        env: Mapping[str, str] | None = None,
        log_name: str = "turn",
        on_event: EventHook | None = None,
    ) -> ClaudeResult:
        """Execute one turn in ``cwd`` and return its structured result.

        ``on_event`` is awaited for every parsed stream-json event as it
        arrives, so the caller can narrate the turn while it is still running.
        It is awaited inline: a slow hook slows the read of stdout, which is
        the honest trade for actions that appear in order.
        """
        session = session_id or str(uuid.uuid4())
        command = self.command(session_id=session, resume=resume)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **(env or {})},
                limit=MAX_LINE_BYTES,
            )
        except FileNotFoundError as exc:
            raise ClaudeCodeError(f"{self.binary} is not installed on this runner") from exc

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        sink, written = self._open_log(log_name, session)
        budget = {"written": written}
        try:
            # stdout and stderr are both pipes with a kernel-sized buffer (on
            # the order of 64 KiB). Reading them one after another means a
            # child that fills stderr before it fills stdout blocks on the
            # write and never reaches EOF on either stream, so the sequential
            # read would hang until the timeout. Draining both concurrently
            # keeps that from being possible; stderr is folded into the same
            # local log stdout already goes to, so a noisy turn's diagnostics
            # sit right next to the output that explains them.
            events, stderr_tail = await asyncio.wait_for(
                asyncio.gather(
                    self._read_events(process.stdout, sink, budget, on_event),
                    self._read_stderr(process.stderr, sink, budget),
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            # The daemon stopped this turn (a cancel request, no progress):
            # the process must not outlive it.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ClaudeCodeError(
                f"claude did not finish within {self.timeout_seconds:.0f}s"
            ) from exc
        finally:
            if sink is not None:
                sink.close()

        code = await process.wait()
        return self._result(events, session_id=session, exit_code=code, stderr=stderr_tail)

    # -- internals -------------------------------------------------------------

    async def _read_events(
        self,
        stream: asyncio.StreamReader,
        sink: Any | None,
        budget: dict[str, int],
        on_event: EventHook | None = None,
    ) -> list[dict[str, Any]]:
        """Consume stream-json, mirroring every line to the local log."""
        events: list[dict[str, Any]] = []
        while True:
            try:
                raw = await stream.readline()
            except ValueError as exc:  # line above the reader limit
                raise ClaudeCodeError("claude emitted an oversized stream-json line") from exc
            if not raw:
                break
            self._log_line(sink, budget, raw)
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                # Not fatal: a non-JSON line is CLI noise, and the result event
                # is what decides the outcome.
                continue
            if isinstance(event, dict):
                events.append(event)
                if on_event is not None:
                    await on_event(event)
        return events

    async def _read_stderr(
        self, stream: asyncio.StreamReader, sink: Any | None, budget: dict[str, int]
    ) -> bytes:
        """Drain stderr as it arrives, mirroring it to the log and keeping a tail."""
        tail = bytearray()
        while True:
            try:
                raw = await stream.readline()
            except ValueError as exc:  # line above the reader limit
                raise ClaudeCodeError("claude emitted an oversized stderr line") from exc
            if not raw:
                break
            self._log_line(sink, budget, raw, prefix="[stderr] ")
            tail += raw
            if len(tail) > STDERR_TAIL_BYTES:
                del tail[: len(tail) - STDERR_TAIL_BYTES]
        return bytes(tail)

    def _log_line(
        self, sink: Any | None, budget: dict[str, int], raw: bytes, prefix: str = ""
    ) -> None:
        if sink is None or budget["written"] >= MAX_LOG_BYTES:
            return
        text = prefix + raw.decode(errors="replace")
        sink.write(text)
        budget["written"] += len(text)

    def _open_log(self, log_name: str, session: str) -> tuple[Any | None, int]:
        if self.log_dir is None:
            return None, 0
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{log_name}-{session}.jsonl"
        # 0600: the stream carries the task's content, and on a shared host that
        # is nobody else's business.
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # The cap applies to the file across resumed turns, not per turn, so the
        # count starts from whatever a prior turn already wrote.
        size = os.fstat(handle).st_size
        return os.fdopen(handle, "w", encoding="utf-8"), size

    def _result(
        self,
        events: Iterable[dict[str, Any]],
        *,
        session_id: str,
        exit_code: int,
        stderr: bytes,
    ) -> ClaudeResult:
        result: dict[str, Any] | None = None
        model = ""
        for event in events:
            if event.get("type") == "result":
                result = event
            elif event.get("type") == "system" and event.get("subtype") == "init":
                model = str(event.get("model") or "")
        if result is None:
            detail = stderr.decode(errors="replace").strip()[:500]
            raise ClaudeCodeError(
                f"claude exited with code {exit_code} without a result"
                + (f": {detail}" if detail else "")
            )
        summary = result.get("result")
        raw_usage = result.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return ClaudeResult(
            # The CLI echoes the session it actually used; trust it over ours so
            # a forked session is still resumable.
            session_id=str(result.get("session_id") or session_id),
            summary=summary if isinstance(summary, str) else "",
            subtype=str(result.get("subtype") or ""),
            is_error=bool(result.get("is_error")) or exit_code != 0,
            turns=int(result.get("num_turns") or 0),
            duration_ms=int(result.get("duration_ms") or 0),
            cost_usd=(
                float(result["total_cost_usd"])
                if result.get("total_cost_usd") is not None
                else None
            ),
            model=model,
            input_tokens=_int_or_none(usage.get("input_tokens")),
            output_tokens=_int_or_none(usage.get("output_tokens")),
        )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def write_mcp_config(path: Path, *, command: str = "control-plane-mcp") -> Path:
    """Declare the Control Plane MCP server for the agent to use.

    No credentials and no server URL are written here: the child inherits the
    runner's environment, and the MCP server resolves its own identity from it
    exactly as it does under a human harness. A config file that carried a token
    would be a second place to leak it from.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mcpServers": {"control-plane": {"type": "stdio", "command": command, "args": []}}}
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
    return path


async def withheld_tools(server: str = MCP_SERVER_NAME) -> list[str]:
    """MCP tool names the agent inside must not call, as the CLI spells them.

    The set itself belongs to the MCP server, which is where the tools are
    declared: keeping a second copy here would mean a tool added there stays
    callable here until somebody remembers to update both.
    """
    from control_plane_mcp.server import withheld_tool_names

    return [f"mcp__{server}__{tool}" for tool in await withheld_tool_names()]
