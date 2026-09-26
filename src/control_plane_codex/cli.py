"""Driving the Codex CLI in non-interactive mode.

This module knows one thing: how to run ``codex exec`` over a working copy and
read back what happened. It holds no Control Plane concepts — the adapter above
it does — so the two can be reasoned about, and tested, apart (same split as
``control_plane_claude``).

The prompt goes in over **stdin**, never as an argument: an argument is visible
in the process table to everyone on the host. ``codex exec -`` forces stdin
mode unambiguously — without the ``-`` marker, a piped stdin is treated as
extra context rather than the prompt itself, which is not the contract this
module wants. The same rule closes the door on credentials: neither the
ChatGPT session token in ``auth.json`` nor an ``OPENAI_API_KEY`` is ever passed
as a flag. Both reach the child through the environment it inherits.

Unlike Claude Code, **the session id is not ours to pick.** ``codex exec``
assigns a fresh thread id itself and reports it as the first line of the
JSON event stream (``thread.started``); there is no flag to request a
specific id for a new session, only ``resume <id>`` to continue one that
already exists. So continuity is checkpointed as early as it *can* be:
- resuming a known session, the id is checkpointed before the process starts,
  exactly like Claude Code (see ``adapter.py``);
- starting a fresh one, the id is checkpointed the instant ``thread.started``
  is parsed, via the ``on_thread_id`` callback below — before the turn has
  done any actual work, but necessarily after the process is already running.
This is a real gap from the "checkpoint before start" rule for the fresh-start
case, forced by the vendor CLI's own contract; a run that dies before its
first stdout line still has nothing to resume, same as it would have if
Codex could take a chosen id.

The raw stream stays **on this host**. Every line is appended to a local log
file, capped at a total size across the file's whole lifetime, including
turns resumed from an earlier session. What travels to the Control Plane is
decided one layer up: the adapter receives every parsed event through
``on_event`` and publishes a bounded, redacted transcript plus the structured
result (ADR-0051). Prompts and reasoning summaries never leave
(harness-protocol §7, §18).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BINARY = "codex"
DEFAULT_TIMEOUT_SECONDS = 3600.0
# Read-only is codex's own default; a coding agent that cannot touch files
# would never do the task, so the adapter opts into edits explicitly.
DEFAULT_SANDBOX = "workspace-write"
# A single line of the JSON event stream is bounded in practice, but a
# runaway tool result should not be able to exhaust the runner's memory
# before the cap applies.
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 32 * 1024 * 1024
# stderr is not published anywhere; this is only how much of it we keep in
# memory to fold into the error message when a turn ends without a result.
STDERR_TAIL_BYTES = 8 * 1024

# Substrings looked for, case-insensitively, in a failed turn's message.
# Nothing here changes behavior today — AR-4's credential pool does not exist
# yet, so a quota-exhausted turn still just fails the run like any other
# error. It is only *named* distinctly, so a future dispatcher can catch
# ``CodexQuotaExhaustedError`` and suspend instead of fail without this
# module changing.
QUOTA_EXHAUSTED_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "usage_limit",
    "quota",
    "429",
)


class CodexError(RuntimeError):
    """The CLI could not be run, or did not finish a turn."""


class CodexQuotaExhaustedError(CodexError):
    """The turn failed because the credential's rolling usage window is spent.

    See the module-level note on ``QUOTA_EXHAUSTED_MARKERS``: this is a label
    for a future consumer, not a behavior change in this adapter.
    """


@dataclass(frozen=True)
class CodexResult:
    """What one non-interactive turn produced.

    ``summary`` is the assistant's final text — the only free-form content
    that is allowed to leave this host, and even then as an artifact the
    adapter truncates.
    """

    session_id: str
    summary: str
    is_error: bool
    duration_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_message: str = ""


@dataclass
class CodexCLI:
    """Runs ``codex exec`` and parses its JSON event stream."""

    binary: str = DEFAULT_BINARY
    model: str | None = None
    sandbox: str = DEFAULT_SANDBOX
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    log_dir: Path | None = None
    extra_args: Sequence[str] = field(default_factory=tuple)

    def command(self, *, resume_session_id: str | None) -> list[str]:
        """Build argv. Nothing secret may appear here — see module docstring."""
        args = [self.binary, "exec", "--json", "--sandbox", self.sandbox]
        if self.model:
            args += ["--model", self.model]
        args += list(self.extra_args)
        # `resume` is a subcommand, not a flag: it takes the target id and,
        # like a fresh run, a trailing prompt argument.
        if resume_session_id:
            args += ["resume", resume_session_id]
        args.append("-")  # force stdin mode, see module docstring
        return args

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        resume_session_id: str | None = None,
        env: Mapping[str, str] | None = None,
        log_name: str = "turn",
        on_thread_id: Callable[[str], Awaitable[None]] | None = None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> CodexResult:
        """Execute one turn in ``cwd`` and return its structured result.

        ``on_thread_id`` is awaited the instant the thread id is known (the
        first event of the stream for a fresh run; immediately, synchronously
        known for a resume) so the caller can checkpoint it without waiting
        for the whole turn to finish.
        """
        command = self.command(resume_session_id=resume_session_id)
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
            raise CodexError(f"{self.binary} is not installed on this runner") from exc

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        # The log file name cannot depend on codex's own session id for a
        # fresh run — it isn't known yet — so a locally generated tag is used
        # purely for the file name, never sent to the process or the server.
        log_tag = resume_session_id or uuid.uuid4().hex[:12]
        sink, written = self._open_log(log_name, log_tag)
        budget = {"written": written}
        start = time.monotonic()
        try:
            # Same reasoning as control_plane_claude: draining stdout and
            # stderr concurrently is what keeps a noisy child from deadlocking
            # against the timeout instead of finishing normally.
            events, stderr_tail = await asyncio.wait_for(
                asyncio.gather(
                    self._read_events(process.stdout, sink, budget, on_thread_id, on_event),
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
            raise CodexError(f"codex did not finish within {self.timeout_seconds:.0f}s") from exc
        finally:
            if sink is not None:
                sink.close()

        code = await process.wait()
        duration_ms = int((time.monotonic() - start) * 1000)
        return self._result(events, exit_code=code, stderr=stderr_tail, duration_ms=duration_ms)

    # -- internals -------------------------------------------------------------

    async def _read_events(
        self,
        stream: asyncio.StreamReader,
        sink: Any | None,
        budget: dict[str, int],
        on_thread_id: Callable[[str], Awaitable[None]] | None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        """Consume the JSON event stream, mirroring every line to the local log."""
        events: list[dict[str, Any]] = []
        seen_thread_id = False
        while True:
            try:
                raw = await stream.readline()
            except ValueError as exc:  # line above the reader limit
                raise CodexError("codex emitted an oversized event line") from exc
            if not raw:
                break
            self._log_line(sink, budget, raw)
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                # Not fatal: a non-JSON line is CLI noise, and the terminal
                # events are what decide the outcome.
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            is_thread_started = event.get("type") == "thread.started"
            if not seen_thread_id and on_thread_id is not None and is_thread_started:
                candidate = event.get("thread_id")
                if isinstance(candidate, str) and candidate:
                    seen_thread_id = True
                    await on_thread_id(candidate)
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
                raise CodexError("codex emitted an oversized stderr line") from exc
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

    def _open_log(self, log_name: str, tag: str) -> tuple[Any | None, int]:
        if self.log_dir is None:
            return None, 0
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{log_name}-{tag}.jsonl"
        # 0600: the stream carries the task's content, and on a shared host
        # that is nobody else's business.
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # The cap applies to the file across resumed turns, not per turn, so
        # the count starts from whatever a prior turn already wrote.
        size = os.fstat(handle).st_size
        return os.fdopen(handle, "w", encoding="utf-8"), size

    def _result(
        self,
        events: Iterable[dict[str, Any]],
        *,
        exit_code: int,
        stderr: bytes,
        duration_ms: int,
    ) -> CodexResult:
        thread_id = ""
        summary = ""
        is_error = exit_code != 0
        error_message = ""
        input_tokens: int | None = None
        output_tokens: int | None = None
        for event in events:
            etype = event.get("type")
            if etype == "thread.started":
                candidate = event.get("thread_id")
                if isinstance(candidate, str) and candidate:
                    thread_id = candidate
            elif etype == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        summary = text
            elif etype == "turn.completed":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    if isinstance(usage.get("input_tokens"), int):
                        input_tokens = usage["input_tokens"]
                    if isinstance(usage.get("output_tokens"), int):
                        output_tokens = usage["output_tokens"]
            elif etype in ("turn.failed", "error"):
                is_error = True
                error_message = _extract_message(event) or error_message

        if not thread_id:
            detail = stderr.decode(errors="replace").strip()[:500]
            raise CodexError(
                f"codex exited with code {exit_code} without a thread id"
                + (f": {detail}" if detail else "")
            )
        if is_error and not error_message:
            error_message = stderr.decode(errors="replace").strip()[:500]
        return CodexResult(
            session_id=thread_id,
            summary=summary,
            is_error=is_error,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_message=error_message,
        )


def _extract_message(event: dict[str, Any]) -> str:
    for key in ("message", "error", "reason"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("message")
            if isinstance(nested, str) and nested:
                return nested
    return ""


def is_quota_exhausted(message: str) -> bool:
    """Heuristic only — see ``QUOTA_EXHAUSTED_MARKERS``."""
    lowered = message.lower()
    return any(marker in lowered for marker in QUOTA_EXHAUSTED_MARKERS)
