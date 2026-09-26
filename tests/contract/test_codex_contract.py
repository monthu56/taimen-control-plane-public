"""Codex adapter contract: the CLI surface we actually depend on (AR-8).

Same reasoning as ``test_claude_code_contract.py``. The unit tests replace the
binary with a script that speaks the same JSON event stream, which proves our
side of the wire. What they cannot catch is the other side moving: a renamed
flag, a `resume` subcommand that changed shape, or an event field that got
renamed would leave every mock-based test green while the runner silently
stops working.

So this file asks the installed CLI itself. The cheap half reads `--help` and
costs nothing. The expensive half runs one real turn and is opt-in, because a
turn spends the operator's rolling usage window (ChatGPT subscription or API
credit) exactly like an interactive session would:

    CP_TEST_CODEX_TURN=1 uv run pytest tests/contract/test_codex_contract.py
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from control_plane_codex.cli import CodexCLI

CLI = os.environ.get("CONTROL_PLANE_CODEX_BINARY", "codex")
INSTALLED = shutil.which(CLI) is not None
RUN_TURN = os.environ.get("CP_TEST_CODEX_TURN") == "1"

pytestmark = pytest.mark.skipif(
    not INSTALLED, reason=f"{CLI} is not installed (adapter is optional on this host)"
)

#: Every flag/subcommand the adapter builds argv from. Kept as a flat list on
#: purpose: the failure message then names the piece that vanished, not
#: "argv changed".
REQUIRED_EXEC_FLAGS = ["--json", "--sandbox", "--model", "resume"]


def help_text(*args: str) -> str:
    result = subprocess.run([CLI, *args], capture_output=True, text=True, timeout=60)
    return result.stdout + result.stderr


@pytest.mark.parametrize("flag", REQUIRED_EXEC_FLAGS)
def test_exec_still_documents_the_flag_the_adapter_builds(flag: str) -> None:
    assert flag in help_text("exec", "--help"), f"{CLI} exec no longer documents {flag}"


def test_sandbox_mode_we_default_to_is_still_offered() -> None:
    assert CodexCLI().sandbox in help_text("exec", "--help")


@pytest.mark.skipif(not RUN_TURN, reason="CP_TEST_CODEX_TURN not set (a real turn costs quota)")
@pytest.mark.asyncio
async def test_one_real_turn_returns_a_parseable_result() -> None:
    """One cheap turn end to end: our parser against the real event stream."""
    with tempfile.TemporaryDirectory() as workdir:
        cli = CodexCLI(binary=CLI, sandbox="read-only")
        result = await cli.run_turn(
            "Reply with exactly: CONTRACT-OK. Do not use any tools.",
            cwd=Path(workdir),
        )

    assert result.session_id
    assert not result.is_error
    assert "CONTRACT-OK" in result.summary
    assert result.duration_ms > 0


@pytest.mark.skipif(not RUN_TURN, reason="CP_TEST_CODEX_TURN not set (a real turn costs quota)")
@pytest.mark.asyncio
async def test_a_resumed_session_keeps_the_same_thread_id() -> None:
    """Continuity depends on this: the id the first turn reports must be
    accepted back by `resume` and must keep naming the same conversation.
    """
    with tempfile.TemporaryDirectory() as workdir:
        cli = CodexCLI(binary=CLI, sandbox="read-only")
        first = await cli.run_turn(
            "Remember the word GRANITE. Reply with OK.",
            cwd=Path(workdir),
        )
        second = await cli.run_turn(
            "What word did I ask you to remember? Reply with the word only.",
            cwd=Path(workdir),
            resume_session_id=first.session_id,
        )

    assert second.session_id == first.session_id
    assert "GRANITE" in second.summary.upper()
