"""Claude Code adapter contract: the CLI surface we actually depend on (AR-3).

The unit tests replace the binary with a script that speaks the same
stream-json, which proves our side of the wire. What they cannot catch is the
other side moving: a renamed flag or a changed result shape would leave every
mock-based test green while the runner silently stops working.

So this file asks the installed CLI itself. The cheap half reads ``--help`` and
costs nothing — it fails the moment a flag we build argv from disappears. The
expensive half runs one real turn and is opt-in, because a turn spends the
operator's subscription window (ADR-0016 §3): the same rolling window their
interactive session needs.

    CP_TEST_CLAUDE_TURN=1 uv run pytest tests/contract/test_claude_code_contract.py
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from control_plane_claude.cli import ClaudeCodeCLI

CLI = os.environ.get("CONTROL_PLANE_CLAUDE_BINARY", "claude")
INSTALLED = shutil.which(CLI) is not None
RUN_TURN = os.environ.get("CP_TEST_CLAUDE_TURN") == "1"

pytestmark = pytest.mark.skipif(
    not INSTALLED, reason=f"{CLI} is not installed (adapter is optional on this host)"
)

#: Every flag the adapter builds argv from. Kept as a flat list on purpose: the
#: failure message then names the flag that vanished, not "argv changed".
REQUIRED_FLAGS = [
    "--print",
    "--output-format",
    "--verbose",
    "--permission-mode",
    "--session-id",
    "--resume",
    "--model",
    "--mcp-config",
    "--strict-mcp-config",
    "--disallowedTools",
]


def help_text() -> str:
    result = subprocess.run([CLI, "--help"], capture_output=True, text=True, timeout=60)
    return result.stdout + result.stderr


@pytest.mark.parametrize("flag", REQUIRED_FLAGS)
def test_cli_still_accepts_the_flag_the_adapter_builds(flag: str) -> None:
    assert flag in help_text(), f"{CLI} no longer documents {flag}"


def test_stream_json_is_still_an_output_format() -> None:
    """The adapter parses stream-json; plain text would give it no result event."""
    assert "stream-json" in help_text()


def test_permission_mode_we_default_to_is_still_offered() -> None:
    assert ClaudeCodeCLI().permission_mode in help_text()


@pytest.mark.skipif(not RUN_TURN, reason="CP_TEST_CLAUDE_TURN not set (a real turn costs quota)")
@pytest.mark.asyncio
async def test_one_real_turn_returns_a_parseable_result() -> None:
    """One cheap turn end to end: our parser against the real event stream."""
    with tempfile.TemporaryDirectory() as workdir:
        cli = ClaudeCodeCLI(binary=CLI, permission_mode="plan")
        result = await cli.run_turn(
            "Reply with exactly: CONTRACT-OK. Do not use any tools.",
            cwd=Path(workdir),
        )

    assert result.session_id
    assert not result.is_error
    assert result.subtype == "success"
    assert "CONTRACT-OK" in result.summary
    # Counters are what the adapter publishes instead of the transcript.
    assert result.turns >= 1
    assert result.duration_ms > 0


@pytest.mark.skipif(not RUN_TURN, reason="CP_TEST_CLAUDE_TURN not set (a real turn costs quota)")
@pytest.mark.asyncio
async def test_a_named_session_can_be_resumed() -> None:
    """Continuity depends on this: the id we pass in must come back resumable."""
    import uuid

    session_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as workdir:
        cli = ClaudeCodeCLI(binary=CLI, permission_mode="plan")
        first = await cli.run_turn(
            "Remember the word GRANITE. Reply with OK.",
            cwd=Path(workdir),
            session_id=session_id,
        )
        second = await cli.run_turn(
            "What word did I ask you to remember? Reply with the word only.",
            cwd=Path(workdir),
            session_id=first.session_id,
            resume=True,
        )

    assert first.session_id == session_id
    assert "GRANITE" in second.summary.upper()


def test_mcp_config_we_write_is_valid_for_the_cli(tmp_path: Path) -> None:
    """`--strict-mcp-config` rejects a malformed file; catch that here, not in prod."""
    from control_plane_claude.cli import write_mcp_config

    path = write_mcp_config(tmp_path / "mcp.json")
    payload = json.loads(path.read_text())

    assert list(payload) == ["mcpServers"]
    server = payload["mcpServers"]["control-plane"]
    assert server["type"] == "stdio"
    # The command must exist on a host that runs the adapter: it is the same
    # package, so an entry point rename would break the passthrough silently.
    assert shutil.which(server["command"]) is not None
