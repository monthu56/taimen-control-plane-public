"""An ``opencode serve`` of one run, started with that run's environment.

The HTTP API of OpenCode (``opencode.py``) has no way to give one prompt its
own environment: the tools of a session — the shell above all — run inside the
server process and inherit what it was started with. A shared server therefore
cannot carry the environment of a run (``env`` of ``.agents/runner.yaml``,
universal-runner FR-018) without handing it to every later run as well. So the
adapter, when told the binary, starts a server for each run with the run's
environment and stops it when the run is over: nothing of one run's
environment outlives it.

The server listens on the loopback only, on a port picked for it; with a
password it demands HTTP basic auth, the same as a shared one. It runs the
model's shell, so it gets the runner's environment without what belongs to
the daemon (:data:`DAEMON_ENV_PREFIXES`): the daemon's API key and identity
stay out of reach of the model. What it prints goes to the log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from control_plane_opencode.opencode import OpenCodeClient, OpenCodeError

logger = logging.getLogger("control_plane_opencode.server")

#: The variable ``opencode serve`` reads its basic-auth password from.
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
#: What of the runner's environment the server does not inherit: the daemon's
#: settings and credentials (``CONTROL_PLANE_API_KEY`` above all), the platform
#: identity (``IAM_*``) and the node's (``FLEET_*``).
DAEMON_ENV_PREFIXES = ("CONTROL_PLANE_", "IAM_", "FLEET_")
#: A line of the server's output longer than this is cut in the log.
MAX_LOG_LINE = 2000


def server_environment(
    environ: Mapping[str, str], env: Mapping[str, str], password: str | None
) -> dict[str, str]:
    """The environment of ``opencode serve``: ``environ`` and ``env`` without the daemon's.

    The password is set only from ``password``, never inherited: a server
    without one gets none.
    """
    merged = {**environ, **env}
    child = {
        name: value
        for name, value in merged.items()
        if not name.startswith(DAEMON_ENV_PREFIXES) and name != PASSWORD_ENV
    }
    if password:
        child[PASSWORD_ENV] = password
    return child


async def _pump(stream: asyncio.StreamReader | None, level: int) -> None:
    """Log what the server writes to ``stream``, a line at a time."""
    if stream is None:
        return
    pending = b""
    while chunk := await stream.read(4096):
        *lines, pending = (pending + chunk).split(b"\n")
        if len(pending) > MAX_LOG_LINE:
            lines, pending = [*lines, pending], b""
        for line in lines:
            _log_line(level, line)
    if pending:
        _log_line(level, pending)


def _log_line(level: int, line: bytes) -> None:
    text = line.decode("utf-8", "replace").rstrip("\r")[:MAX_LOG_LINE]
    logger.log(level, "opencode serve: %s", text)


def _free_port(hostname: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((hostname, 0))
        port: int = probe.getsockname()[1]
        return port


class OpenCodeServer:
    """Starts ``opencode serve`` for one run and stops it afterwards."""

    def __init__(
        self,
        binary: str = "opencode",
        *,
        hostname: str = "127.0.0.1",
        password: str | None = None,
        cwd: Path | None = None,
        startup_timeout: float = 30.0,
        stop_timeout: float = 10.0,
        request_timeout: float = 300.0,
    ) -> None:
        self.binary = binary
        self.hostname = hostname
        self.password = password
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.request_timeout = request_timeout

    @contextlib.asynccontextmanager
    async def running(self, env: Mapping[str, str]) -> AsyncIterator[OpenCodeClient]:
        """A client of a server whose process has ``env`` on top of the runner's.

        ``env`` is taken as it is: the caller has already dropped what a run
        may not set. The daemon's own variables are left out
        (:func:`server_environment`); the password, when there is one, is set last.
        """
        port = _free_port(self.hostname)
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "serve",
                "--hostname",
                self.hostname,
                "--port",
                str(port),
                cwd=str(self.cwd) if self.cwd is not None else None,
                env=server_environment(os.environ, env, self.password),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise OpenCodeError(f"{self.binary} is not installed on this runner") from exc
        # Read both pipes all the time: a full pipe would stall the server.
        pumps = [
            asyncio.create_task(_pump(process.stdout, logging.INFO)),
            asyncio.create_task(_pump(process.stderr, logging.WARNING)),
        ]
        client = OpenCodeClient(
            f"http://{self.hostname}:{port}",
            password=self.password,
            timeout=self.request_timeout,
        )
        try:
            await self._wait_healthy(client, process)
            yield client
        finally:
            await client.aclose()
            await self._stop(process)
            # The pipes close with the process; what is left in them is logged.
            _, unfinished = await asyncio.wait(pumps, timeout=self.stop_timeout)
            for task in unfinished:
                task.cancel()

    async def _wait_healthy(
        self, client: OpenCodeClient, process: asyncio.subprocess.Process
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout
        while True:
            if process.returncode is not None:
                raise OpenCodeError(f"opencode serve exited with {process.returncode} on start")
            try:
                if (await client.health()).get("healthy") is not False:
                    return
            except OpenCodeError:
                pass  # not listening yet
            if loop.time() >= deadline:
                raise OpenCodeError(
                    f"opencode serve did not become healthy in {self.startup_timeout:g}s"
                )
            await asyncio.sleep(0.1)

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self.stop_timeout)
        except TimeoutError:
            logger.warning("opencode serve did not stop in %ss; killing it", self.stop_timeout)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
