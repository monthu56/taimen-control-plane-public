"""Claude Code harness adapter (AR-3, ADR-0016 §1).

The properties under test are the ones that make an autonomous executor safe to
leave running: the prompt and the credential never reach argv, the session id is
checkpointed before the process that owns it starts, a failed turn fails the run
instead of publishing a half-done result, and what leaves the host is a bounded,
redacted transcript plus tool-call actions — never the prompt, never hidden
reasoning (ADR-0051).

The CLI is replaced by a script that speaks the same stream-json, so the
subprocess layer — argv, stdin, parsing, timeout — is exercised for real.
"""

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from control_plane_agent.workspace import assert_portable
from control_plane_claude.adapter import CHECKPOINT_KIND, ClaudeCodeAdapter
from control_plane_claude.cli import ClaudeCodeCLI, ClaudeCodeError, write_mcp_config
from control_plane_mcp.server import EVIDENCE_TOOLS, withheld_tool_names

RESULT_LINE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 3,
    "duration_ms": 1200,
    "total_cost_usd": 0.42,
    "usage": {"input_tokens": 120, "output_tokens": 30},
    "result": "Changed two files; tests pass.",
}
INIT_LINE = {
    "type": "system",
    "subtype": "init",
    "model": "claude-opus-5",
    "tools": ["Bash", "Read"],
}
# One turn as `claude -p --output-format stream-json` actually emits it: a
# thinking block, a tool call, its result, then the visible text.
CONVERSATION = [
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "private", "signature": "x"}],
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "pytest -q", "description": "run tests"},
                }
            ],
        },
    },
    {
        "type": "user",
        "timestamp": "2026-09-10T05:00:00.000Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [{"type": "text", "text": "3 passed in /Users/runner/work"}],
                }
            ],
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Tests pass; see /home/runner/x."}],
        },
    },
]


def fake_cli(
    tmp_path: Path,
    *,
    result: dict[str, Any] | None,
    exit_code: int = 0,
    conversation: list[dict[str, Any]] = (),
) -> Path:
    """A stand-in for `claude` that records argv and stdin, then replies."""
    script = tmp_path / "fake-claude"
    lines = [json.dumps(INIT_LINE), *(json.dumps(e) for e in conversation)]
    if result is not None:
        lines.append(json.dumps(result))
    payload = "\n".join(lines)
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
        self.action_kwargs: list[dict[str, Any]] = []
        self.finished: list[tuple[str, str]] = []

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
        self.action_kwargs.append(kwargs)
        return {"id": f"action-{len(self.actions)}"}

    async def finish_action(self, run_id: str, action_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"finish_action:{kwargs['status']}")
        self.finished.append((action_id, kwargs["status"]))
        return {"id": action_id}


TASK = {"id": "t-1", "publicId": "TASK-000042", "title": "Do the thing", "description": "Details."}
RUN = {"id": "run-1", "attempt": 1}


def adapter_for(script: Path, tmp_path: Path, **kwargs: Any) -> ClaudeCodeAdapter:
    cli = ClaudeCodeCLI(binary=str(script), log_dir=tmp_path / "logs", **kwargs)
    return ClaudeCodeAdapter(cli)


@pytest.mark.asyncio
async def test_prompt_goes_over_stdin_and_never_into_argv(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, result=RESULT_LINE)
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
async def test_session_is_checkpointed_before_the_process_starts(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, result=RESULT_LINE)
    client = FakeClient()

    await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    # A session id known only to a process that then dies is unresumable, so the
    # checkpoint must precede the turn — and the turn is what records an action.
    assert client.calls.index("checkpoint:started") < client.calls.index("record_action")
    started = client.written[0]["data"]
    assert started["kind" if False else "claudeSessionId"]
    assert started["resumed"] is False
    assert client.written[0]["kind"] == CHECKPOINT_KIND
    assert "--session-id" in (tmp_path / "argv.txt").read_text()


@pytest.mark.asyncio
async def test_next_attempt_resumes_the_recorded_session(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, result=RESULT_LINE)
    client = FakeClient(
        checkpoints=[{"kind": CHECKPOINT_KIND, "data": {"claudeSessionId": "sess-earlier"}}]
    )

    await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    argv = (tmp_path / "argv.txt").read_text()
    assert "--resume sess-earlier" in argv
    assert "--session-id" not in argv
    assert client.written[0]["data"]["resumed"] is True


@pytest.mark.asyncio
async def test_failed_turn_fails_the_run_instead_of_publishing_a_result(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, result={**RESULT_LINE, "is_error": True, "subtype": "error_max"})
    client = FakeClient()

    with pytest.raises(ClaudeCodeError):
        await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    assert "finish_action:failed" in client.calls


@pytest.mark.asyncio
async def test_a_turn_without_a_result_is_an_error_not_an_empty_summary(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, result=None, exit_code=1)
    client = FakeClient()

    with pytest.raises(ClaudeCodeError):
        await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    # The failure is recorded where the next attempt will look for it.
    assert any(item["data"].get("phase") == "failed" for item in client.written)


@pytest.mark.asyncio
async def test_raw_stream_stays_on_the_runner_and_a_bounded_transcript_is_published(
    tmp_path: Path,
) -> None:
    script = fake_cli(tmp_path, result=RESULT_LINE, conversation=CONVERSATION)
    client = FakeClient()

    artifacts = await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    logs = list((tmp_path / "logs").glob("TASK-000042-*.jsonl"))
    assert len(logs) == 1
    assert json.loads(logs[0].read_text().splitlines()[0])["type"] == "system"
    assert stat.S_IMODE(logs[0].stat().st_mode) == 0o600

    summary, transcript = artifacts
    assert set(summary.content or {}) == {"summary"}
    assert set(summary.metadata) == {
        "harnessType",
        "claudeSessionId",
        "turns",
        "durationMs",
        "model",
        "costUsd",
        "inputTokens",
        "outputTokens",
    }
    assert transcript.type == "transcript"
    content = transcript.content or {}
    assert content["schema"] == "agent-transcript/1"
    assert content["model"] == "claude-opus-5" and content["tools"] == ["Bash", "Read"]
    assert [e["kind"] for e in content["entries"]] == ["tool_call", "tool_result", "assistant"]
    assert content["entries"][0]["tool"] == "Bash"
    assert content["entries"][1]["at"] == "2026-09-10T05:00:00.000Z"
    assert content["final"]["text"] == "Changed two files; tests pass."
    assert content["stats"]["hiddenReasoningBlocks"] == 1
    assert content["usage"]["costUsd"] == 0.42 and content["usage"]["inputTokens"] == 120
    # Neither the prompt nor the thinking block nor a host path reaches the document.
    text = json.dumps(content)
    assert "private" not in text and "Details." not in text
    assert "/Users/" not in text and "/home/" not in text and "<path>" in text
    assert transcript.metadata["toolCalls"] == 1 and transcript.metadata["claudeSessionId"]
    for spec in artifacts:
        assert_portable({"content": spec.content, "metadata": spec.metadata})


@pytest.mark.asyncio
async def test_tool_calls_are_narrated_as_run_actions_while_the_turn_runs(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, result=RESULT_LINE, conversation=CONVERSATION)
    client = FakeClient()

    await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    assert client.actions == [("claude-code.turn", "started"), ("tool.Bash", "started")]
    tool = client.action_kwargs[1]
    assert tool["external_reference"].endswith("#call/1")
    assert tool["metadata"] == {"tool": "Bash", "call": 1, "summary": "pytest -q"}
    # The tool's action is finished by its result, the turn's by the result event.
    assert client.finished == [("action-2", "completed"), ("action-1", "completed")]


@pytest.mark.asyncio
async def test_trace_settings_can_turn_publishing_off(tmp_path: Path) -> None:
    from control_plane_agent.trace import TraceSettings

    script = fake_cli(tmp_path, result=RESULT_LINE, conversation=CONVERSATION)
    client = FakeClient()
    cli = ClaudeCodeCLI(binary=str(script), log_dir=tmp_path / "logs")
    adapter = ClaudeCodeAdapter(cli, trace=TraceSettings(transcript=False, actions=False))

    artifacts = await adapter.execute(TASK, RUN, client, None)

    assert [a.type for a in artifacts] == ["report"]
    assert client.actions == [("claude-code.turn", "started")]


@pytest.mark.asyncio
async def test_timeout_kills_the_process(tmp_path: Path) -> None:
    script = tmp_path / "hanging-claude"
    script.write_text("#!/bin/sh\ncat > /dev/null\nsleep 30\n")
    script.chmod(0o755)
    client = FakeClient()
    adapter = adapter_for(script, tmp_path, timeout_seconds=0.5)

    with pytest.raises(ClaudeCodeError, match="did not finish"):
        await adapter.execute(TASK, RUN, client, None)


@pytest.mark.asyncio
async def test_noisy_stderr_does_not_block_stdout(tmp_path: Path) -> None:
    """stdout and stderr are both pipes with a kernel-sized buffer (~64 KiB on
    Linux). A child that fills stderr past that before it writes stdout blocks
    on the write and never reaches EOF on either stream — so reading stdout to
    completion and only then draining stderr would hang until the timeout.
    """
    script = tmp_path / "noisy-claude"
    payload = "\n".join([json.dumps(INIT_LINE), json.dumps(RESULT_LINE)])
    script.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        "head -c 200000 /dev/zero | tr '\\0' 'x' 1>&2\n"
        f"cat <<'JSON'\n{payload}\nJSON\n"
    )
    script.chmod(0o755)
    cli = ClaudeCodeCLI(binary=str(script), log_dir=tmp_path / "logs", timeout_seconds=5)

    result = await cli.run_turn("prompt", cwd=tmp_path, session_id="s1")

    assert result.summary == RESULT_LINE["result"]


@pytest.mark.asyncio
async def test_missing_binary_is_reported_as_such(tmp_path: Path) -> None:
    client = FakeClient()
    adapter = adapter_for(tmp_path / "not-installed", tmp_path)

    with pytest.raises(ClaudeCodeError, match="not installed"):
        await adapter.execute(TASK, RUN, client, None)


def test_mcp_config_carries_no_credential_and_no_server(tmp_path: Path) -> None:
    path = write_mcp_config(tmp_path / "mcp.json")

    payload = json.loads(path.read_text())
    server = payload["mcpServers"]["control-plane"]

    assert server["command"] == "control-plane-mcp"
    # The child inherits the runner's environment; a config file that repeated
    # the token would be a second place to leak it from.
    assert "env" not in server
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_authoritative_commands_are_withheld_from_the_agent_inside(tmp_path: Path) -> None:
    script = fake_cli(tmp_path, result=RESULT_LINE)
    client = FakeClient()
    adapter = adapter_for(script, tmp_path, mcp_config_path=write_mcp_config(tmp_path / "mcp.json"))

    await adapter.execute(TASK, RUN, client, None)

    argv = (tmp_path / "argv.txt").read_text()
    assert "--strict-mcp-config" in argv
    for tool in ("cp_complete_run", "cp_fail_run", "cp_claim_task", "cp_approve"):
        assert f"mcp__control-plane__{tool}" in argv
    # Reading context and leaving evidence stays available.
    for tool in ("cp_get_run_context", "cp_create_artifact", "cp_checkpoint"):
        assert f"mcp__control-plane__{tool}" not in argv


@pytest.mark.asyncio
async def test_withheld_set_is_derived_from_the_registry_not_a_copy_of_it() -> None:
    """A tool added to the MCP server must not stay callable by omission."""
    from control_plane_mcp.server import mcp

    withheld = set(await withheld_tool_names())
    declared = {tool.name for tool in await mcp.list_tools()}

    assert withheld <= declared
    # Anything that is neither read-only nor evidence is withheld — including a
    # tool nobody remembered to annotate.
    for tool in await mcp.list_tools():
        read_only = bool(tool.annotations and tool.annotations.read_only_hint)
        expected = not read_only and tool.name not in EVIDENCE_TOOLS
        assert (tool.name in withheld) is expected, tool.name


@pytest.mark.asyncio
async def test_every_tool_declares_whether_it_mutates() -> None:
    """The annotation is the contract clients read; silence is not an answer."""
    from control_plane_mcp.server import mcp

    unannotated = [tool.name for tool in await mcp.list_tools() if tool.annotations is None]

    assert unannotated == []


def test_environment_never_reaches_argv(tmp_path: Path) -> None:
    cli = ClaudeCodeCLI(binary="claude", model="opus")

    argv = cli.command(session_id="s", resume=False)

    joined = " ".join(argv)
    for secret in ("CLAUDE_CODE_OAUTH_TOKEN", "IAM_PLATFORM_ACCESS_TOKEN", "iam_pat_"):
        assert secret not in joined
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "sentinel") not in joined


def test_daemon_resolves_the_adapter_by_name() -> None:
    from control_plane_agent.main import EXTERNAL_ADAPTERS, build_adapter

    assert "claude-code" in EXTERNAL_ADAPTERS
    assert isinstance(build_adapter("claude-code"), ClaudeCodeAdapter)
    with pytest.raises(LookupError):
        build_adapter("no-such-adapter")


def test_importing_the_daemon_does_not_drag_in_the_vendor_adapter() -> None:
    """That is what keeps `control-plane-agent` usable without Claude Code."""
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import control_plane_agent.main, sys; print('control_plane_claude' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert probe.stdout.strip() == "False"


@pytest.mark.asyncio
async def test_summary_with_a_host_path_does_not_fail_the_run(tmp_path: Path) -> None:
    """A model that has been working in a directory names paths as a matter of
    course. The daemon's assert_portable is a last line of defence, and a last
    line that trips on ordinary output turns finished work into a failed run —
    which is exactly what happened on TASK-000040, losing its report. So the
    path is redacted at the source and the artifact stays publishable.
    """
    noisy = dict(RESULT_LINE)
    noisy["result"] = "Cloned the sdk to /opt/runner/worktrees/platform-auth-sdk and ran the tests."
    script = fake_cli(tmp_path, result=noisy)

    artifacts = await adapter_for(script, tmp_path).execute(TASK, RUN, FakeClient(), None)

    summary = (artifacts[0].content or {})["summary"]
    assert "/opt/runner" not in summary
    assert "<path>" in summary
    # The sentence still reads: redaction removes the path, not the meaning.
    assert "ran the tests" in summary
    # And the guard the daemon applies before publishing now passes.
    assert_portable({"name": artifacts[0].name, "content": artifacts[0].content}, where="artifact")


async def test_agent_conventions_file_is_appended_to_the_prompt(tmp_path: Path) -> None:
    """CONTROL_PLANE_CLAUDE_PROMPT_FILE: соглашения агента (четвёртый слой, «Agent
    conventions») идут в каждый prompt, чтобы не стоить по кругу ревью на ветку.
    Файл читается на каждом запуске — правка без перезапуска runner'а; отсутствие
    файла не роняет run."""
    script = fake_cli(tmp_path, result=RESULT_LINE)
    conventions = tmp_path / "conventions.md"
    conventions.write_text("Имена скиллов — по разделу 14; uv.lock не коммитить.\n")
    adapter = adapter_for(script, tmp_path)
    adapter.prompt_file = conventions

    await adapter.execute(TASK, RUN, FakeClient(), None)

    stdin = (tmp_path / "stdin.txt").read_text()
    assert "### Agent conventions" in stdin
    assert "Repository conventions" not in stdin
    assert "uv.lock не коммитить" in stdin

    adapter.prompt_file = tmp_path / "missing.md"
    await adapter.execute(TASK, RUN, FakeClient(), None)
    assert "Agent conventions" not in (tmp_path / "stdin.txt").read_text()


class FakeClientWithPack(FakeClient):
    async def get_working_context(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("context")
        return {
            "operational": {},
            "memoryStatus": "ok",
            "memory": {
                "trace_id": "ctx-1",
                "sections": [
                    {
                        "kind": "relevant_facts",
                        "items": [
                            {
                                "text": "Memory reads the workspace namespace too",
                                "source_path": "control-plane://events/e1",
                            }
                        ],
                    }
                ],
            },
        }


async def test_recalled_context_reaches_the_prompt(tmp_path: Path) -> None:
    """TAI-ADR-0042 п.6: пакет контекста больше не выбрасывается — раздел
    «Контекст задачи» в prompt; без пакета — одна строка, run не падает."""
    script = fake_cli(tmp_path, result=RESULT_LINE)

    await adapter_for(script, tmp_path).execute(TASK, RUN, FakeClientWithPack(), None)
    stdin = (tmp_path / "stdin.txt").read_text()
    assert "## Контекст задачи" in stdin
    assert "Memory reads the workspace namespace too" in stdin
    assert "control-plane://events/e1" in stdin

    await adapter_for(script, tmp_path).execute(TASK, RUN, FakeClient(), None)
    stdin = (tmp_path / "stdin.txt").read_text()
    assert "контекст памяти недоступен: unavailable" in stdin


# -- the environment of a run (universal-runner U009, FR-018) -------------------

ENV_PROBES = ("CP_TEST_DATABASE_URL", "ANTHROPIC_BASE_URL", "http_proxy", "CONTROL_PLANE_RUN_ID")


def env_probe_cli(tmp_path: Path) -> Path:
    """A `claude` that writes what it sees of ENV_PROBES, one `NAME=value` a line."""
    script = tmp_path / "fake-claude"
    probes = "".join(
        f'printf "%s\\n" "{name}=${{{name}-<unset>}}" >> "{tmp_path}/env.txt"\n'
        for name in ENV_PROBES
    )
    script.write_text(
        "#!/bin/sh\n"
        f'rm -f "{tmp_path}/env.txt"\n'
        + probes
        + "cat > /dev/null\n"
        + f"cat <<'JSON'\n{json.dumps(RESULT_LINE)}\nJSON\n"
    )
    script.chmod(0o755)
    return script


def seen_env(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "env.txt").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


@pytest.mark.asyncio
async def test_run_environment_reaches_the_process_and_not_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ENV_PROBES:
        monkeypatch.delenv(name, raising=False)
    adapter = adapter_for(env_probe_cli(tmp_path), tmp_path)
    url = "postgresql+psycopg://u:p@db:5432/t"

    await adapter.execute(TASK, RUN, FakeClient(), None, env={"CP_TEST_DATABASE_URL": url})

    assert seen_env(tmp_path)["CP_TEST_DATABASE_URL"] == url
    assert "CP_TEST_DATABASE_URL" not in os.environ

    await adapter.execute(TASK, {"id": "run-2", "attempt": 1}, FakeClient(), None)

    assert seen_env(tmp_path)["CP_TEST_DATABASE_URL"] == "<unset>"


@pytest.mark.asyncio
async def test_run_environment_cannot_set_reserved_names_or_the_run_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: the parser refuses these, and the adapter drops them again."""
    for name in ENV_PROBES:
        monkeypatch.delenv(name, raising=False)
    adapter = adapter_for(env_probe_cli(tmp_path), tmp_path)

    await adapter.execute(
        TASK,
        RUN,
        FakeClient(),
        None,
        env={
            "ANTHROPIC_BASE_URL": "https://elsewhere.example",
            "http_proxy": "http://elsewhere.example:3128",
            "CONTROL_PLANE_RUN_ID": "someone-elses-run",
            "CP_TEST_DATABASE_URL": "kept",
        },
    )

    seen = seen_env(tmp_path)
    assert seen["ANTHROPIC_BASE_URL"] == "<unset>"
    assert seen["http_proxy"] == "<unset>"
    assert seen["CONTROL_PLANE_RUN_ID"] == RUN["id"]
    assert seen["CP_TEST_DATABASE_URL"] == "kept"


@pytest.mark.asyncio
async def test_run_ids_are_merged_over_the_run_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order of the merge holds even for an environment the filter let through."""
    for name in ENV_PROBES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("control_plane_claude.adapter.run_environment", dict)
    adapter = adapter_for(env_probe_cli(tmp_path), tmp_path)

    await adapter.execute(
        TASK, RUN, FakeClient(), None, env={"CONTROL_PLANE_RUN_ID": "someone-elses-run"}
    )

    assert seen_env(tmp_path)["CONTROL_PLANE_RUN_ID"] == RUN["id"]


@pytest.mark.asyncio
async def test_value_with_nul_is_dropped_and_the_process_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ENV_PROBES:
        monkeypatch.delenv(name, raising=False)
    adapter = adapter_for(env_probe_cli(tmp_path), tmp_path)

    await adapter.execute(
        TASK, RUN, FakeClient(), None, env={"CP_TEST_DATABASE_URL": "postgresql://h/d\x00"}
    )

    assert seen_env(tmp_path)["CP_TEST_DATABASE_URL"] == "<unset>"


@pytest.mark.asyncio
async def test_empty_run_environment_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CP_TEST_DATABASE_URL", "from-the-runner")
    adapter = adapter_for(env_probe_cli(tmp_path), tmp_path)

    await adapter.execute(TASK, RUN, FakeClient(), None, env={})

    assert seen_env(tmp_path)["CP_TEST_DATABASE_URL"] == "from-the-runner"


class FakeClientBehindARestart(FakeClient):
    """The core restarts just as the turn ends: the proxy answers 502."""

    async def finish_action(self, run_id: str, action_id: str, **kwargs: Any) -> dict[str, Any]:
        from control_plane_client import ControlPlaneError

        self.calls.append(f"finish_action:{kwargs['status']}")
        raise ControlPlaneError("http_error", "Unexpected server error", status=502)


@pytest.mark.asyncio
async def test_a_bad_gateway_on_finishing_the_turn_does_not_fail_finished_work(
    tmp_path: Path,
) -> None:
    """TASK-001138: the 502 used to escape the adapter and the daemon failed the run."""
    script = fake_cli(tmp_path, result=RESULT_LINE)
    client = FakeClientBehindARestart()

    artifacts = await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)

    assert "finish_action:completed" in client.calls
    assert artifacts[0].content == {"summary": "Changed two files; tests pass."}
    assert "checkpoint:finished" in client.calls


class FakeClientThatLostTheClaim(FakeClient):
    """The core answers the bookkeeping itself: the claim is no longer ours."""

    async def finish_action(self, run_id: str, action_id: str, **kwargs: Any) -> dict[str, Any]:
        from control_plane_client import StaleClaimError

        self.calls.append(f"finish_action:{kwargs['status']}")
        raise StaleClaimError("stale_claim", "claim lost", status=409)


@pytest.mark.asyncio
async def test_an_answer_of_the_core_on_finishing_the_turn_is_not_swallowed(
    tmp_path: Path,
) -> None:
    """Only an unreachable core is tolerated; a lost claim still stops the run."""
    from control_plane_client import StaleClaimError

    script = fake_cli(tmp_path, result=RESULT_LINE)
    client = FakeClientThatLostTheClaim()

    with pytest.raises(StaleClaimError):
        await adapter_for(script, tmp_path).execute(TASK, RUN, client, None)
    assert "finish_action:completed" in client.calls
