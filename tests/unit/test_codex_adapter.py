"""Codex harness adapter (AR-8).

The properties under test mirror ``test_claude_adapter.py`` — the prompt and
the credential never reach argv, a failed turn fails the run instead of
publishing a half-done result, and nothing said in the conversation leaves
the host — plus one that is specific to Codex: unlike Claude Code, a fresh
session's id is not ours to choose, so continuity can only be checkpointed
the instant the CLI reports it, not literally before the process starts (see
``control_plane_codex/cli.py`` and ``adapter.py`` for why).

The CLI is replaced by a script that speaks the same JSON event stream, so
the subprocess layer — argv, stdin, parsing, timeout — is exercised for real.
"""

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from control_plane_codex.adapter import CHECKPOINT_KIND, CodexAdapter
from control_plane_codex.cli import CodexCLI, CodexError, CodexQuotaExhaustedError

TASK = {"id": "t-1", "publicId": "TASK-000042", "title": "Do the thing", "description": "Details."}
RUN = {"id": "run-1", "attempt": 1}


def fake_cli(
    tmp_path: Path,
    *,
    thread_id: str | None = "thread-abc",
    agent_message: str | None = "Changed two files; tests pass.",
    turn_failed: str | None = None,
    exit_code: int = 0,
) -> Path:
    """A stand-in for `codex` that records argv and stdin, then replies."""
    script = tmp_path / "fake-codex"
    events: list[dict[str, Any]] = []
    if thread_id is not None:
        events.append({"type": "thread.started", "thread_id": thread_id})
    events.append({"type": "turn.started"})
    if agent_message is not None:
        events.append(
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": agent_message},
            }
        )
    if turn_failed is not None:
        events.append({"type": "turn.failed", "message": turn_failed})
    else:
        events.append(
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 42}}
        )
    payload = "\n".join(json.dumps(event) for event in events)
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s" "$*" > "{tmp_path}/argv.txt"\n'
        f'cat > "{tmp_path}/stdin.txt"\n'
        f"cat <<'JSON'\n{payload}\nJSON\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script


class FakeClient:
    """Records the calls the adapter makes, in order."""

    def __init__(self, checkpoints: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[str] = []
        self.checkpoints = checkpoints or []
        self.written: list[dict[str, Any]] = []
        self.actions: list[tuple[str, str]] = []

    async def list_checkpoints(self, run_id: str) -> dict[str, Any]:
        self.calls.append("list_checkpoints")
        return {"items": self.checkpoints}

    async def create_checkpoint(self, run_id: str, *, kind: str, data: Any) -> dict[str, Any]:
        self.calls.append(f"checkpoint:{data.get('phase')}")
        self.written.append({"kind": kind, "data": data})
        return {"id": "cp"}

    async def get_working_context(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("context")
        return {"operational": {"project": {"statusKey": "active"}}}

    async def record_action(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("record_action")
        self.actions.append((kwargs["action"], kwargs.get("status", "")))
        return {"id": "action-1"}

    async def finish_action(self, run_id: str, action_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"finish_action:{kwargs['status']}")
        return {"id": action_id}


def adapter_for(script: Path, tmp_path: Path, **kwargs: Any) -> CodexAdapter:
    cli = CodexCLI(binary=str(script), log_dir=tmp_path / "logs", **kwargs)
    return CodexAdapter(cli)


@pytest.mark.asyncio
async def test_prompt_goes_over_stdin_and_never_into_argv(tmp_path: Path) -> None:
    script = fake_cli(tmp_path)
    client = FakeClient()

    artifacts = await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    argv = (tmp_path / "argv.txt").read_text()
    stdin = (tmp_path / "stdin.txt").read_text()
    assert "Do the thing" not in argv
    assert "Details." not in argv
    # The task text arrived — just not where `ps` can read it.
    assert "TASK-000042" in stdin
    assert "Details." in stdin
    assert artifacts[0].content == {"summary": "Changed two files; tests pass."}


@pytest.mark.asyncio
async def test_new_session_id_is_checkpointed_as_soon_as_known(tmp_path: Path) -> None:
    """No prior checkpoint exists, so the id cannot be known before the
    process starts (unlike Claude Code — see the module docstrings). It must
    still be captured the instant the stream reports it, not only once the
    whole turn finishes.
    """
    script = fake_cli(tmp_path, thread_id="thread-fresh")
    client = FakeClient()

    await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    started = next(item for item in client.written if item["data"].get("phase") == "started")
    assert started["kind"] == CHECKPOINT_KIND
    assert started["data"]["codexSessionId"] == "thread-fresh"
    assert started["data"]["resumed"] is False


@pytest.mark.asyncio
async def test_resumed_session_is_checkpointed_before_the_process_starts(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, thread_id="sess-earlier")
    client = FakeClient(
        checkpoints=[{"kind": CHECKPOINT_KIND, "data": {"codexSessionId": "sess-earlier"}}]
    )

    await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    # Here, unlike a fresh session, the id is already known — so the checkpoint
    # precedes both the action record and the process start.
    assert client.calls.index("checkpoint:started") < client.calls.index("record_action")
    argv = (tmp_path / "argv.txt").read_text()
    assert "resume sess-earlier" in argv
    first_checkpoint = next(
        item for item in client.written if item["data"].get("phase") == "started"
    )
    assert first_checkpoint["data"]["codexSessionId"] == "sess-earlier"
    assert first_checkpoint["data"]["resumed"] is True


@pytest.mark.asyncio
async def test_failed_turn_fails_the_run_instead_of_publishing_a_result(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, turn_failed="boom")
    client = FakeClient()

    with pytest.raises(CodexError):
        await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    assert "finish_action:failed" in client.calls


@pytest.mark.asyncio
async def test_quota_exhaustion_is_named_distinctly_but_still_fails_the_run(tmp_path: Path) -> None:
    """AR-4's credential pool does not exist yet, so this still fails the run
    like any other error — the distinct exception type is only there for a
    future dispatcher to catch (see cli.py's QUOTA_EXHAUSTED_MARKERS note).
    """
    script = fake_cli(tmp_path, turn_failed="usage limit reached, try again later")
    client = FakeClient()

    with pytest.raises(CodexQuotaExhaustedError):
        await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    assert "finish_action:failed" in client.calls


@pytest.mark.asyncio
async def test_a_turn_without_a_thread_id_is_an_error_not_an_empty_summary(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, thread_id=None, exit_code=1)
    client = FakeClient()

    with pytest.raises(CodexError):
        await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    # The failure is recorded where the next attempt will look for it.
    assert any(item["data"].get("phase") == "failed" for item in client.written)


@pytest.mark.asyncio
async def test_raw_stream_stays_on_the_runner_and_a_bounded_transcript_is_published(
    tmp_path: Path,
) -> None:
    script = fake_cli(tmp_path, thread_id="thread-log")
    client = FakeClient()

    artifacts = await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    logs = list((tmp_path / "logs").glob("TASK-000042-*.jsonl"))
    assert len(logs) == 1
    assert json.loads(logs[0].read_text().splitlines()[0])["type"] == "thread.started"
    assert stat.S_IMODE(logs[0].stat().st_mode) == 0o600
    summary, transcript = artifacts
    assert set(summary.content or {}) == {"summary"}
    assert set(summary.metadata) == {
        "harnessType",
        "codexSessionId",
        "durationMs",
        "inputTokens",
        "outputTokens",
    }
    assert transcript.type == "transcript"
    content = transcript.content or {}
    assert content["schema"] == "agent-transcript/1"
    assert content["sessionId"] == "thread-log"
    assert [e["kind"] for e in content["entries"]] == ["assistant"]
    assert content["final"]["text"] == "Changed two files; tests pass."
    assert transcript.metadata["codexSessionId"] == "thread-log"


@pytest.mark.asyncio
async def test_commands_and_mcp_calls_become_tool_calls_and_actions(tmp_path: Path) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-tools"},
        {"type": "turn.started"},
        {
            "type": "item.started",
            "item": {
                "id": "i1",
                "type": "command_execution",
                "command": "pytest -q",
                "status": "in_progress",
            },
        },
        {"type": "item.completed", "item": {"id": "i2", "type": "reasoning", "text": "private"}},
        {
            "type": "item.completed",
            "item": {
                "id": "i1",
                "type": "command_execution",
                "command": "pytest -q",
                "aggregated_output": "3 passed",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "i3",
                "type": "mcp_tool_call",
                "server": "cp",
                "tool": "cp_context",
                "arguments": {},
                "error": "denied",
                "status": "failed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "i4",
                "type": "file_change",
                "changes": [{"path": "a.py", "kind": "update"}],
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"id": "i5", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
    ]
    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/bin/sh\ncat > /dev/null\ncat <<'JSON'\n"
        + "\n".join(json.dumps(e) for e in events)
        + "\nJSON\n"
    )
    script.chmod(0o755)
    client = FakeClient()

    artifacts = await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    content = artifacts[1].content or {}
    kinds = [(e["kind"], e.get("tool")) for e in content["entries"]]
    assert kinds == [
        ("tool_call", "command_execution"),
        ("tool_result", None),
        ("tool_call", "mcp.cp.cp_context"),
        ("tool_result", None),
        ("tool_call", "file_change"),
        ("tool_result", None),
        ("assistant", None),
    ]
    assert content["entries"][1]["output"].startswith("[exit 0]")
    assert content["entries"][3]["isError"] is True
    assert content["stats"]["hiddenReasoningBlocks"] == 1
    assert "private" not in json.dumps(content)
    assert [a for a in client.actions if a[0].startswith("tool.")] == [
        ("tool.command_execution", "started"),
        ("tool.mcp.cp.cp_context", "started"),
        ("tool.file_change", "started"),
    ]


@pytest.mark.asyncio
async def test_credential_class_travels_on_the_artifact_when_configured(tmp_path: Path) -> None:
    """Nothing chooses a credential_class yet (AR-4) — but a runner configured
    with one should have it visible on what it publishes.
    """
    script = fake_cli(tmp_path)
    client = FakeClient()
    cli = CodexCLI(binary=str(script), log_dir=tmp_path / "logs")
    adapter = CodexAdapter(cli, credential_class="chatgpt-subscription")

    artifacts = await adapter.execute(TASK, RUN, client, None)

    assert artifacts[0].metadata["credentialClass"] == "chatgpt-subscription"


@pytest.mark.asyncio
async def test_timeout_kills_the_process(tmp_path: Path) -> None:
    script = tmp_path / "hanging-codex"
    script.write_text("#!/bin/sh\ncat > /dev/null\nsleep 30\n")
    script.chmod(0o755)
    client = FakeClient()
    adapter = adapter_for(script, tmp_path, timeout_seconds=0.5)

    with pytest.raises(CodexError, match="did not finish"):
        await adapter.execute(TASK, RUN, client, None)


@pytest.mark.asyncio
async def test_noisy_stderr_does_not_block_stdout(tmp_path: Path) -> None:
    """stdout and stderr are both pipes with a kernel-sized buffer (~64 KiB on
    Linux). A child that fills stderr past that before it writes stdout blocks
    on the write and never reaches EOF on either stream — so reading stdout to
    completion and only then draining stderr would hang until the timeout.
    """
    payload = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-noisy"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": "done"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )
    script = tmp_path / "noisy-codex"
    script.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        "head -c 200000 /dev/zero | tr '\\0' 'x' 1>&2\n"
        f"cat <<'JSON'\n{payload}\nJSON\n"
    )
    script.chmod(0o755)
    cli = CodexCLI(binary=str(script), log_dir=tmp_path / "logs", timeout_seconds=5)

    result = await cli.run_turn("prompt", cwd=tmp_path)

    assert result.summary == "done"
    assert result.session_id == "thread-noisy"


@pytest.mark.asyncio
async def test_missing_binary_is_reported_as_such(tmp_path: Path) -> None:
    client = FakeClient()
    adapter = adapter_for(tmp_path / "not-installed", tmp_path)

    with pytest.raises(CodexError, match="not installed"):
        await adapter.execute(TASK, RUN, client, None)


def test_environment_never_reaches_argv() -> None:
    cli = CodexCLI(binary="codex", model="gpt-5-codex")

    argv = cli.command(resume_session_id=None)

    joined = " ".join(argv)
    for secret in ("OPENAI_API_KEY", "IAM_PLATFORM_ACCESS_TOKEN", "iam_pat_"):
        assert secret not in joined
    assert os.environ.get("OPENAI_API_KEY", "sentinel") not in joined


def test_resume_is_a_subcommand_with_no_session_id_flag_for_a_fresh_run() -> None:
    """Codex assigns a fresh session's id itself — there is no flag to name
    one, only `resume <id>` to continue an existing one. See cli.py.
    """
    cli = CodexCLI(binary="codex")

    fresh = cli.command(resume_session_id=None)
    resumed = cli.command(resume_session_id="sess-1")

    assert "resume" not in fresh
    assert "resume" in resumed
    assert "sess-1" in resumed


def test_daemon_resolves_the_adapter_by_name() -> None:
    from control_plane_agent.main import EXTERNAL_ADAPTERS, build_adapter

    assert "codex" in EXTERNAL_ADAPTERS
    assert isinstance(build_adapter("codex"), CodexAdapter)


def test_importing_the_daemon_does_not_drag_in_the_vendor_adapter() -> None:
    """That is what keeps `control-plane-agent` usable without Codex installed."""
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import control_plane_agent.main, sys; print('control_plane_codex' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert probe.stdout.strip() == "False"
