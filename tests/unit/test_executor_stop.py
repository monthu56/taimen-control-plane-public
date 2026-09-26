"""A stopped turn takes its process with it (control_plane_agent.supervision).

The daemon stops an executor by cancelling it; the CLI adapters must not
leave the vendor process running behind a cancelled turn.
"""

import asyncio
import os
import stat
from pathlib import Path

import pytest

from control_plane_claude.cli import ClaudeCodeCLI
from control_plane_codex.cli import CodexCLI


def _fake_binary(tmp_path: Path) -> tuple[Path, Path]:
    pid_file = tmp_path / "pid"
    script = tmp_path / "fake-cli"
    script.write_text(f'#!/bin/sh\necho $$ > "{pid_file}"\nexec sleep 30\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, pid_file


def _read_pid(pid_file: Path) -> int | None:
    text = pid_file.read_text().strip() if pid_file.exists() else ""
    return int(text) if text else None


async def _pid(pid_file: Path) -> int:
    for _ in range(200):
        pid = _read_pid(pid_file)
        if pid is not None:
            return pid
        await asyncio.sleep(0.01)
    raise AssertionError("the fake CLI did not start")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("vendor", ["claude", "codex"])
async def test_a_cancelled_turn_kills_its_process(tmp_path: Path, vendor: str) -> None:
    binary, pid_file = _fake_binary(tmp_path)
    if vendor == "claude":
        turn = ClaudeCodeCLI(binary=str(binary)).run_turn("work", cwd=tmp_path)
    else:
        turn = CodexCLI(binary=str(binary)).run_turn("work", cwd=tmp_path)
    task = asyncio.ensure_future(turn)
    pid = await _pid(pid_file)
    assert _alive(pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(pid)
