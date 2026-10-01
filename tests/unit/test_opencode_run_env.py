"""The environment of a run reaches OpenCode's process and dies with the run (U009, FR-018).

OpenCode's tools run inside ``opencode serve``, so the adapter starts a server
per run with the run's environment (``control_plane_opencode.server``). The
binary is replaced by a script that serves the documented paths and answers a
prompt with what it sees of a few variables, so the process layer — argv,
environment, health wait, stop — runs for real.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from control_plane_agent.services import declared_services_at
from control_plane_opencode.main import OpenCodeAdapter
from control_plane_opencode.opencode import OpenCodeClient
from control_plane_opencode.server import (
    MAX_LOG_LINE,
    OpenCodeServer,
    _pump,
    server_environment,
)
from control_plane_opencode.server import PASSWORD_ENV as OPENCODE_PASSWORD

PROBES = (
    "CP_TEST_DATABASE_URL",
    "ANTHROPIC_BASE_URL",
    "OPENCODE_SERVER_PASSWORD",
    "CONTROL_PLANE_API_KEY",
    "CONTROL_PLANE_SERVER",
    "IAM_PLATFORM_ACCESS_TOKEN",
    "FLEET_SERVICES_URL",
    "RUNNER_ORDINARY",
)

FAKE_SERVE = """\
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
assert args[0] == "serve", args
host = args[args.index("--hostname") + 1]
port = int(args[args.index("--port") + 1])
with open(os.environ["FAKE_OPENCODE_PIDS"], "a") as pids:
    pids.write(f"{os.getpid()}\\n")
print(f"opencode server listening on {port}", flush=True)
print("warning from the fake server", file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/global/health":
            return self.reply(200, {"healthy": True})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/session":
            return self.reply(200, {"id": "ses_1"})
        if self.path.endswith("/message"):
            seen = {name: os.environ.get(name) for name in PROBES}
            text = {"type": "text", "text": json.dumps(seen)}
            return self.reply(200, {"info": {"id": "msg_1"}, "parts": [text]})
        return self.reply(200, {})


HTTPServer((host, port), Handler).serve_forever()
"""


def fake_opencode(tmp_path: Path) -> Path:
    script = tmp_path / "fake-opencode"
    body = FAKE_SERVE.replace("PROBES", repr(PROBES))
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return script


class FakeControlPlane:
    """Hands out one task per cycle and records what the adapter reports."""

    def __init__(self) -> None:
        self.runs = 0
        self.summaries: list[str] = []
        self.failures: list[str] = []
        self.succeeded: list[str] = []

    async def open_session(self, **kwargs: Any) -> dict[str, Any]:
        return {"id": "session-1"}

    async def list_available_work(self, **kwargs: Any) -> dict[str, Any]:
        return {"items": [{"id": "t-1", "publicId": "TASK-000042", "title": "Do the thing"}]}

    async def claim_task(self, task_id: str, session_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"id": "claim-1", "fencingToken": 1}

    async def start_run(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.runs += 1
        return {"id": f"run-{self.runs}", "attempt": 1}

    async def get_working_context(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    async def list_checkpoints(self, run_id: str) -> dict[str, Any]:
        return {"items": []}

    async def record_action(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"id": "action-1"}

    async def create_artifact(self, **kwargs: Any) -> dict[str, Any]:
        self.summaries.append(kwargs["content"]["summary"])
        return {"id": "artifact-1"}

    async def succeed_run(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        self.succeeded.append(run_id)
        return {}

    async def fail_run(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        self.failures.append(kwargs["failure_reason"])
        return {}

    def __getattr__(self, name: str) -> Any:
        # Heartbeats, checkpoints and the rest of the bookkeeping.
        async def accept(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        return accept


def env_source(by_run: Mapping[str, Mapping[str, str]]) -> Any:
    async def source(task: dict[str, Any], run: dict[str, Any]) -> Mapping[str, str]:
        return by_run.get(run["id"], {})

    return source


def seen(summary: str) -> dict[str, str | None]:
    result: dict[str, str | None] = json.loads(summary)
    return result


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in PROBES:
        monkeypatch.delenv(name, raising=False)
    pids = tmp_path / "pids.txt"
    monkeypatch.setenv("FAKE_OPENCODE_PIDS", str(pids))
    return pids


def adapter(
    client: FakeControlPlane,
    tmp_path: Path,
    by_run: Mapping[str, Mapping[str, str]],
    *,
    password: str | None = None,
) -> OpenCodeAdapter:
    return OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        OpenCodeClient("http://127.0.0.1:9"),  # the shared server; must not be used
        server=OpenCodeServer(str(fake_opencode(tmp_path)), password=password, cwd=tmp_path),
        run_env=env_source(by_run),
    )


def started_servers(pid_file: Path) -> list[int]:
    if not pid_file.exists():
        return []
    return [int(line) for line in pid_file.read_text().split()]


def process_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.asyncio
async def test_run_environment_reaches_the_server_and_not_the_next_run(
    tmp_path: Path, clean_env: Path
) -> None:
    client = FakeControlPlane()
    url = "postgresql+psycopg://u:p@db:5432/t"
    by_run = {"run-1": {"CP_TEST_DATABASE_URL": url}, "run-2": {"OTHER": "y"}}
    opencode = adapter(client, tmp_path, by_run)

    assert await opencode.run_once()
    assert await opencode.run_once()

    assert client.failures == []
    assert client.succeeded == ["run-1", "run-2"]
    assert seen(client.summaries[0])["CP_TEST_DATABASE_URL"] == url
    assert seen(client.summaries[1])["CP_TEST_DATABASE_URL"] is None
    assert "CP_TEST_DATABASE_URL" not in os.environ
    # One server per run, each stopped when its run was over.
    pids = started_servers(clean_env)
    assert len(set(pids)) == 2
    assert all(process_is_gone(pid) for pid in pids)


@pytest.mark.asyncio
async def test_run_environment_cannot_set_reserved_names(tmp_path: Path, clean_env: Path) -> None:
    client = FakeControlPlane()
    env = {
        "ANTHROPIC_BASE_URL": "https://elsewhere.example",
        "OPENCODE_SERVER_PASSWORD": "chosen-by-the-repository",
        "CP_TEST_DATABASE_URL": "kept",
    }
    opencode = adapter(client, tmp_path, {"run-1": env}, password="the-runners")

    assert await opencode.run_once()

    result = seen(client.summaries[0])
    assert result["ANTHROPIC_BASE_URL"] is None
    assert result["OPENCODE_SERVER_PASSWORD"] == "the-runners"
    assert result["CP_TEST_DATABASE_URL"] == "kept"


@pytest.mark.asyncio
async def test_daemon_credentials_do_not_reach_the_server(
    tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server runs the model's shell: the daemon's key and identity stay out of it."""
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "cp_daemon_key")
    monkeypatch.setenv("CONTROL_PLANE_SERVER", "https://cp.example")
    monkeypatch.setenv("IAM_PLATFORM_ACCESS_TOKEN", "iam-token")
    monkeypatch.setenv("FLEET_SERVICES_URL", "https://fleet.example")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "from-the-daemons-environment")
    monkeypatch.setenv("RUNNER_ORDINARY", "inherited")
    client = FakeControlPlane()
    opencode = adapter(client, tmp_path, {"run-1": {"CP_TEST_DATABASE_URL": "x"}})

    assert await opencode.run_once()

    result = seen(client.summaries[0])
    assert result["CONTROL_PLANE_API_KEY"] is None
    assert result["CONTROL_PLANE_SERVER"] is None
    assert result["IAM_PLATFORM_ACCESS_TOKEN"] is None
    assert result["FLEET_SERVICES_URL"] is None
    # Without a password of its own the server gets none, not the daemon's.
    assert result["OPENCODE_SERVER_PASSWORD"] is None
    assert result["RUNNER_ORDINARY"] == "inherited"
    assert result["CP_TEST_DATABASE_URL"] == "x"


@pytest.mark.asyncio
async def test_value_with_nul_is_dropped_and_the_run_goes_on(
    tmp_path: Path, clean_env: Path
) -> None:
    client = FakeControlPlane()
    env = {"BROKEN": "a\x00b", "CP_TEST_DATABASE_URL": "kept"}
    opencode = adapter(client, tmp_path, {"run-1": env})

    assert await opencode.run_once()

    assert client.failures == []
    assert seen(client.summaries[0])["CP_TEST_DATABASE_URL"] == "kept"


@pytest.mark.asyncio
async def test_server_output_goes_to_the_log(
    tmp_path: Path,
    clean_env: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alembic's fileConfig in the migration tests disables loggers that exist by then.
    monkeypatch.setattr(logging.getLogger("control_plane_opencode.server"), "disabled", False)
    client = FakeControlPlane()
    opencode = adapter(client, tmp_path, {"run-1": {"CP_TEST_DATABASE_URL": "x"}})

    with caplog.at_level(logging.INFO, logger="control_plane_opencode.server"):
        assert await opencode.run_once()

    assert "opencode server listening on" in caplog.text
    assert "warning from the fake server" in caplog.text


@pytest.mark.asyncio
async def test_run_without_an_environment_starts_no_server(tmp_path: Path, clean_env: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        return httpx.Response(
            200, json={"info": {"id": "msg_1"}, "parts": [{"type": "text", "text": "shared"}]}
        )

    client = FakeControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        OpenCodeClient("http://opencode", transport=httpx.MockTransport(handler)),
        server=OpenCodeServer(str(fake_opencode(tmp_path)), cwd=tmp_path),
        run_env=env_source({}),
    )

    assert await opencode.run_once()

    assert client.summaries == ["shared"]
    assert started_servers(clean_env) == []


@pytest.mark.asyncio
async def test_shared_server_refuses_a_run_with_its_own_environment() -> None:
    """A shared server would keep the variables for every later run: fail instead."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(500)

    client = FakeControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        OpenCodeClient("http://opencode", transport=httpx.MockTransport(handler)),
        run_env=env_source({"run-1": {"CP_TEST_DATABASE_URL": "x"}}),
    )

    assert not await opencode.run_once()

    assert requests == []
    assert client.succeeded == []
    assert len(client.failures) == 1 and "CONTROL_PLANE_OPENCODE_BINARY" in client.failures[0]


@pytest.mark.asyncio
async def test_shared_server_serves_a_run_without_an_environment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        return httpx.Response(
            200, json={"info": {"id": "msg_1"}, "parts": [{"type": "text", "text": "done"}]}
        )

    client = FakeControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        OpenCodeClient("http://opencode", transport=httpx.MockTransport(handler)),
        run_env=env_source({}),
    )

    assert await opencode.run_once()

    assert client.summaries == ["done"]


@pytest.mark.asyncio
async def test_server_that_never_starts_fails_the_run(tmp_path: Path, clean_env: Path) -> None:
    broken = tmp_path / "broken-opencode"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    client = FakeControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        OpenCodeClient("http://127.0.0.1:9"),
        server=OpenCodeServer(str(broken), startup_timeout=5),
        run_env=env_source({"run-1": {"CP_TEST_DATABASE_URL": "x"}}),
    )

    assert not await opencode.run_once()

    assert client.succeeded == []
    assert len(client.failures) == 1 and "exited with 3" in client.failures[0]


@pytest.mark.asyncio
async def test_missing_binary_fails_the_run(tmp_path: Path) -> None:
    client = FakeControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        OpenCodeClient("http://127.0.0.1:9"),
        server=OpenCodeServer(str(tmp_path / "no-such-opencode")),
        run_env=env_source({"run-1": {"CP_TEST_DATABASE_URL": "x"}}),
    )

    assert not await opencode.run_once()

    assert len(client.failures) == 1 and "not installed" in client.failures[0]


# -- the pieces of the server, one by one ---------------------------------------------


def test_server_environment_leaves_the_daemons_variables_out() -> None:
    environ = {
        "PATH": "/usr/bin",
        "CONTROL_PLANE_API_KEY": "k",
        "IAM_CREDENTIAL_MODE": "token",
        "FLEET_NODE": "n",
        "OPENCODE_SERVER_PASSWORD": "inherited",
    }

    child = server_environment(environ, {"CP_TEST_DATABASE_URL": "x"}, None)

    assert child == {"PATH": "/usr/bin", "CP_TEST_DATABASE_URL": "x"}


def test_server_environment_drops_the_daemons_variables_from_the_run_too() -> None:
    env = {"CONTROL_PLANE_API_KEY": "k", OPENCODE_PASSWORD: "chosen", "KEPT": "y"}

    assert server_environment({}, env, "the-runners") == {
        "KEPT": "y",
        OPENCODE_PASSWORD: "the-runners",
    }


def test_server_environment_does_not_touch_its_inputs() -> None:
    environ = {"CONTROL_PLANE_API_KEY": "k"}
    env = {"KEPT": "y"}

    server_environment(environ, env, "p")

    assert environ == {"CONTROL_PLANE_API_KEY": "k"} and env == {"KEPT": "y"}


@pytest.mark.asyncio
async def test_pump_logs_whole_lines_cuts_long_ones_and_keeps_the_tail(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("control_plane_opencode.server"), "disabled", False)
    stream = asyncio.StreamReader()
    stream.feed_data(b"first\r\nsec")
    stream.feed_data(b"ond\n" + b"x" * (MAX_LOG_LINE * 3) + b"\nbroken \xff tail")
    stream.feed_eof()

    with caplog.at_level(logging.INFO, logger="control_plane_opencode.server"):
        await _pump(stream, logging.INFO)

    messages = [record.getMessage() for record in caplog.records]
    assert messages[:2] == ["opencode serve: first", "opencode serve: second"]
    assert messages[-1] == "opencode serve: broken � tail"
    assert all(len(m) <= len("opencode serve: ") + MAX_LOG_LINE for m in messages)


# -- services of runner.yaml (U013): this harness asks the node for none -------------

RUNNER_YAML = """\
version: 1
services:
  db:
    template: postgres-16
    env:
      CP_TEST_DATABASE_URL: "postgresql://{user}:{password}@{host}:{port}/{database}"
"""


class BlockingControlPlane(FakeControlPlane):
    """Also records how a task goes to a person (``settle_blocked``)."""

    def __init__(self) -> None:
        super().__init__()
        self.outputs: list[Any] = []
        self.updates: list[dict[str, Any]] = []
        self.comments: list[str] = []
        self.released: list[str] = []

    async def fail_run(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        self.outputs.append(kwargs.get("output"))
        return await super().fail_run(run_id, **kwargs)

    async def get_task_transitions(self, task_id: str) -> dict[str, Any]:
        return {
            "targets": [{"status": "blocked", "systemStatusCategory": "blocked", "route": "update"}]
        }

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "version": 3}

    async def update_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {}

    async def add_task_comment(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.comments.append(kwargs["body"])
        return {}

    async def release_claim(self, claim_id: str, **kwargs: Any) -> dict[str, Any]:
        self.released.append(kwargs["reason"])
        return {}


def _repository(path: Path, runner_yaml: str | None) -> Path:
    path.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=path,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n")
    if runner_yaml is not None:
        (path / ".agents").mkdir()
        (path / ".agents" / "runner.yaml").write_text(runner_yaml)
    git("add", "-A")
    git("commit", "-qm", "base")
    return path


def _shared(requests: list[str] | None = None) -> OpenCodeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request.url.path)
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_1"})
        return httpx.Response(
            200, json={"info": {"id": "msg_1"}, "parts": [{"type": "text", "text": "done"}]}
        )

    return OpenCodeClient("http://opencode", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_declared_services_without_a_source_block_the_task(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo", RUNNER_YAML)
    client = BlockingControlPlane()
    requests: list[str] = []
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        _shared(requests),
        declared_services=lambda: declared_services_at(repository, "HEAD"),
    )

    assert await opencode.run_once()

    assert requests == []  # OpenCode was never asked
    assert client.succeeded == [] and client.summaries == []
    assert client.failures == ["test_services_not_offered"]
    [output] = client.outputs
    assert "db" in output["reason"] and "CONTROL_PLANE_AGENT_SERVICES=host" in output["reason"]
    assert str(tmp_path) not in output["reason"]
    assert [u["status"] for u in client.updates] == ["blocked"]
    assert client.updates[0]["fencing_token"] == 1
    [comment] = client.comments
    assert "test_services_not_offered" in comment
    assert client.released == ["test_services_not_offered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("runner_yaml", [None, "version: 1\n"])
async def test_no_declared_services_run_as_before(tmp_path: Path, runner_yaml: str | None) -> None:
    repository = _repository(tmp_path / "repo", runner_yaml)
    client = BlockingControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        _shared(),
        declared_services=lambda: declared_services_at(repository, "HEAD"),
    )

    assert await opencode.run_once()

    assert client.summaries == ["done"] and client.failures == []


@pytest.mark.asyncio
async def test_outside_a_repository_there_are_no_services(tmp_path: Path) -> None:
    client = BlockingControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        _shared(),
        declared_services=lambda: declared_services_at(tmp_path, "HEAD"),
    )

    assert await opencode.run_once()

    assert client.summaries == ["done"]


@pytest.mark.asyncio
async def test_invalid_runner_yaml_blocks_the_task(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo", "version: 1\nservices: [db]\n")
    client = BlockingControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        _shared(),
        declared_services=lambda: declared_services_at(repository, "HEAD"),
    )

    assert await opencode.run_once()

    assert client.failures == ["runner_config_invalid"]
    assert client.released == ["runner_config_invalid"]


@pytest.mark.asyncio
async def test_host_mode_checks_nothing(tmp_path: Path) -> None:
    """``declared_services=None`` is what ``main`` passes in the mode ``host``."""
    client = BlockingControlPlane()
    opencode = OpenCodeAdapter(client, _shared())  # type: ignore[arg-type]

    assert await opencode.run_once()

    assert client.summaries == ["done"] and client.failures == []


@pytest.mark.asyncio
async def test_a_source_of_the_run_environment_is_trusted_with_the_services() -> None:
    def never() -> Mapping[str, Any]:
        raise AssertionError("with run_env the services are the source's business")

    client = BlockingControlPlane()
    opencode = OpenCodeAdapter(
        client,  # type: ignore[arg-type]
        _shared(),
        run_env=env_source({}),
        declared_services=never,
    )

    assert await opencode.run_once()

    assert client.summaries == ["done"]


@pytest.mark.asyncio
async def test_every_run_reads_the_services_anew(tmp_path: Path) -> None:
    calls: list[int] = []

    def declared() -> Mapping[str, Any]:
        calls.append(1)
        return {}

    client = BlockingControlPlane()
    opencode = OpenCodeAdapter(client, _shared(), declared_services=declared)  # type: ignore[arg-type]

    assert await opencode.run_once()
    assert await opencode.run_once()

    assert len(calls) == 2
