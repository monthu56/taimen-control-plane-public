"""Test services of a run, asked of the node (universal-runner U013).

A repository names the services its tests need in ``services`` of
``.agents/runner.yaml`` (``runner_config.py``), each by a template of the node.
Before the executor starts, the daemon asks the ``fleet-services`` listener of
its node for them with the container token, waits for the answer and for the
services to accept connections, and hands the ``env`` of ``runner.yaml`` with
the address and the credentials filled in to the executor of this run only
(FR-017, FR-018, FR-020 of the feature).

The wire is the node-local contract of the fleet (``fleet_api.models``,
``docs/node.md`` of the fleet repository); a pinned copy of its OpenAPI is
``tests/fixtures/fleet_services_openapi.json`` and the fake listener of the
tests is held to it:

- ``POST /api/v1/service-requests`` — ``202`` with ``status: pending``;
- ``GET /api/v1/service-requests/{requestId}?wait=<0-60>`` — long poll until
  ``ready``, ``unavailable`` or ``rejected``; ``404`` once the listener
  restarted and lost the request — asked again;
- ``POST .../{requestId}/heartbeat`` every ``SERVICES_HEARTBEAT_SECONDS`` while
  the run lives, each holding the services ``SERVICES_LEASE_SECONDS``; a
  ``404`` means the hold is lost and the run goes on without it;
- ``POST .../{requestId}/release`` when the run ends — before ``ready`` it
  withdraws the request.

How a run ends when it gets no services:

- ``test_services_unavailable`` — the node had no budget or the services did
  not get ready (or accept connections) in time: the run fails and the task
  goes back to the queue (:class:`ServicesUnavailable`);
- ``service_template_unavailable``, ``service_quota_exceeded`` — the node has
  no such template, or the run asks for more than a replica may hold: blocked
  (:class:`ServicesBlocked`), a person fixes ``runner.yaml`` or the node;
- ``test_services_not_offered`` — the runner cannot ask: no listener
  (``FLEET_SERVICES_URL``/``FLEET_SERVICES_TOKEN_FILE`` are given only to an
  executor kind with ``services: true``), no readable token, or an executor
  that takes no run environment: blocked;
- ``test_services_refused`` — the listener refused the token, the pair or the
  request, or answered outside the contract: blocked;
- ``runner_config_invalid`` — ``runner.yaml`` of the base does not parse:
  blocked.

Neither the token nor a password is ever logged, put into an exception or
into durable state: the environment of the run lives in memory for the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx

from control_plane_agent.runner_config import (
    RUNNER_CONFIG_PATH,
    RunnerConfigError,
    ServiceCredentials,
    ServiceSpec,
    parse_runner_config,
)
from control_plane_agent.workspace import Workspace

logger = logging.getLogger("control_plane_agent.services")

# Names and numbers of the contract (fleet_api.models); the contract test holds
# them to the pinned OpenAPI of the listener.
ENV_SERVICES_URL = "FLEET_SERVICES_URL"
ENV_SERVICES_TOKEN_FILE = "FLEET_SERVICES_TOKEN_FILE"
ENV_REPLICA = "FLEET_REPLICA"
ENV_AGENT_KEY = "CONTROL_PLANE_AGENT_KEY"
REQUESTS_PATH = "/api/v1/service-requests"
MAX_WAIT_SECONDS = 60
SERVICE_WAIT_DEFAULT_SECONDS = 600
SERVICE_WAIT_MIN_SECONDS = 30
SERVICE_WAIT_MAX_SECONDS = 1800
SERVICES_LEASE_SECONDS = 300
SERVICES_HEARTBEAT_SECONDS = 60
# The listener answers a request the node left unanswered this long after its
# deadline itself (unavailable); the daemon waits that long and a poll more.
LISTENER_GRACE_SECONDS = 30

#: Where the services of a run come from: ``fleet`` (the default) asks the node
#: when the listener is configured and stops a run that declares services
#: otherwise; ``host`` hands nothing and leaves the executor with what the host
#: provides — for a runner whose node gives no services yet (stage 1 of
#: universal-runner, static test databases).
ENV_SERVICES_MODE = "CONTROL_PLANE_AGENT_SERVICES"
MODES = ("fleet", "host")

TEMPLATE_UNAVAILABLE = "service_template_unavailable"
QUOTA_EXCEEDED = "service_quota_exceeded"
UNAVAILABLE = "test_services_unavailable"
NOT_OFFERED = "test_services_not_offered"
REFUSED = "test_services_refused"
RUNNER_CONFIG_INVALID = "runner_config_invalid"

#: How long a ready service may take to accept a connection.
CONNECT_SECONDS = 60.0
#: Pause before asking again after a transport error or ``503``; ``429`` waits
#: :data:`THROTTLED_SECONDS` (the listener allows one signal a second).
RETRY_SECONDS = 5.0
THROTTLED_SECONDS = 1.0
#: Attempts of a heartbeat or a release that meets ``429``.
SIGNAL_ATTEMPTS = 3
#: How long one release may take: the run is over, and a listener that hangs
#: must not hold its working copy for the timeout of a long poll.
RELEASE_TIMEOUT_SECONDS = 10.0

_FINAL = ("ready", "unavailable", "rejected")


class ServicesBlocked(Exception):
    """The run cannot get its services, and waiting will not change that.

    ``code`` is the failure reason of the run, ``reason`` the words for the
    person the task goes to; neither names a token, a password or a local path.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class ServicesUnavailable(Exception):
    """No services this time (budget, readiness): the task goes back to the queue."""

    code = UNAVAILABLE

    def __init__(self, reason: str) -> None:
        super().__init__(f"{UNAVAILABLE}: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class ServicesSettings:
    """Where the listener is and which replica asks."""

    url: str
    token_file: Path
    agent_key: str
    replica: int
    wait_seconds: int = SERVICE_WAIT_DEFAULT_SECONDS

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"{ENV_SERVICES_URL} is not an http(s) address")
        if not self.agent_key:
            raise ValueError(f"no agent key: {ENV_AGENT_KEY} is not set")
        if isinstance(self.replica, bool) or not 0 <= self.replica <= 99:
            raise ValueError(f"{ENV_REPLICA} is not a replica number 0-99")
        if not SERVICE_WAIT_MIN_SECONDS <= self.wait_seconds <= SERVICE_WAIT_MAX_SECONDS:
            raise ValueError(
                f"wait is {SERVICE_WAIT_MIN_SECONDS}-{SERVICE_WAIT_MAX_SECONDS} seconds"
            )

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str], agent_key: str | None = None
    ) -> ServicesSettings | None:
        """The settings the node gave this container, or None without a listener.

        The node sets ``FLEET_SERVICES_URL`` and ``FLEET_SERVICES_TOKEN_FILE``
        together, only for an executor kind with ``services: true``; one
        without the other, or either without the replica, is a broken
        deployment (``ValueError``), not a runner without services.
        """
        url = environ.get(ENV_SERVICES_URL, "").strip()
        token_file = environ.get(ENV_SERVICES_TOKEN_FILE, "").strip()
        if not url and not token_file:
            return None
        if not url or not token_file:
            raise ValueError(
                f"{ENV_SERVICES_URL} and {ENV_SERVICES_TOKEN_FILE} come together; "
                f"only {ENV_SERVICES_URL if url else ENV_SERVICES_TOKEN_FILE} is set"
            )
        replica = environ.get(ENV_REPLICA, "").strip()
        if not replica.isascii() or not replica.isdigit():
            raise ValueError(f"{ENV_REPLICA} is not a replica number 0-99")
        return cls(
            url=url.rstrip("/"),
            token_file=Path(token_file),
            agent_key=agent_key or environ.get(ENV_AGENT_KEY, "").strip(),
            replica=int(replica),
        )


def services_mode(environ: Mapping[str, str]) -> str:
    """``CONTROL_PLANE_AGENT_SERVICES``: ``fleet`` unless set; anything else is a ``ValueError``."""
    mode = environ.get(ENV_SERVICES_MODE, "").strip() or "fleet"
    if mode not in MODES:
        raise ValueError(f"{ENV_SERVICES_MODE} is one of {', '.join(MODES)}, got {mode!r}")
    return mode


def declared_services(workspace: Workspace) -> Mapping[str, ServiceSpec]:
    """``services`` of ``runner.yaml`` at the base the task branch was cut from.

    Never the branch's own head: a change of ``runner.yaml`` on the task
    branch takes effect once it is reviewed and merged (FR-009). No file —
    no services.
    """
    return declared_services_at(workspace.path, workspace.base_revision or workspace.base_commit)


def declared_services_at(path: Path, revision: str) -> Mapping[str, ServiceSpec]:
    """``services`` of ``runner.yaml`` at ``revision`` of the repository at ``path``.

    No file, or no repository at all — no services.
    """
    spec = f"{revision}:{RUNNER_CONFIG_PATH}"
    if _git(path, "cat-file", "-e", spec).returncode != 0:
        return {}
    shown = _git(path, "show", spec)
    if shown.returncode != 0:
        raise ServicesBlocked(
            RUNNER_CONFIG_INVALID, f"{RUNNER_CONFIG_PATH} of the base cannot be read"
        )
    try:
        config = parse_runner_config(shown.stdout)
    except RunnerConfigError as exc:
        raise ServicesBlocked(RUNNER_CONFIG_INVALID, str(exc)) from exc
    return config.services


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@dataclass
class RunServices:
    """The services one run holds: its environment and how to let them go."""

    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: The values of ``env`` a check's output must not show
    #: (``ServiceSpec.render_secrets``): the passwords, and what carries them.
    secrets: tuple[str, ...] = field(default=(), repr=False)
    request_id: str | None = None
    _heartbeats: asyncio.Task[None] | None = field(default=None, repr=False)
    _release: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    async def close(self) -> None:
        """Stop the heartbeats and release the services; safe to call twice.

        Never raises for a heartbeat loop that broke: the release still goes
        out, and whoever closes goes on to free the working copy.
        """
        heartbeats, self._heartbeats = self._heartbeats, None
        release, self._release = self._release, None
        try:
            if heartbeats is not None:
                heartbeats.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeats
        except Exception as exc:
            logger.warning(
                "heartbeats of request %s failed: %s", self.request_id, type(exc).__name__
            )
        finally:
            if release is not None:
                await release()


@dataclass
class _Request:
    """The request of one acquisition, as far as it got."""

    id: str | None = None
    # A final answer other than ready came: there is nothing left to release.
    settled: bool = False


class FleetServicesClient:
    """Asks the ``fleet-services`` listener of this node for a run's services."""

    def __init__(
        self,
        settings: ServicesSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        connect: Callable[[str, int], Awaitable[bool]] | None = None,
        connect_seconds: float = CONNECT_SECONDS,
        heartbeat_seconds: float = SERVICES_HEARTBEAT_SECONDS,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._connect = connect or _accepts_connection
        self.connect_seconds = connect_seconds
        self.heartbeat_seconds = heartbeat_seconds

    # -- one run ---------------------------------------------------------------

    async def acquire(self, services: Mapping[str, ServiceSpec]) -> RunServices:
        """Ask for ``services``, wait until they are ready and reachable.

        Raises :class:`ServicesUnavailable` (back to the queue) or
        :class:`ServicesBlocked`; whatever it asked for is released first.
        """
        body = {
            "agentKey": self.settings.agent_key,
            "replica": self.settings.replica,
            "services": {name: {"template": spec.template} for name, spec in services.items()},
            "waitSeconds": self.settings.wait_seconds,
        }
        names = ", ".join(sorted(services))
        deadline = (
            self._clock() + self.settings.wait_seconds + LISTENER_GRACE_SECONDS + MAX_WAIT_SECONDS
        )
        client = self._http()
        request = _Request()
        try:
            answer = await self._answer(client, body, deadline, request)
            credentials = self._credentials(answer, services)
            await self._reachable(credentials)
        except BaseException:
            # Before ready a release withdraws the request; after it, it ends
            # the hold: either way nothing waits for a run that will not come.
            if request.id is not None and not request.settled:
                await self._signal(client, request.id, "release")
            await client.aclose()
            raise
        request_id = request.id
        assert request_id is not None  # a ready answer names its request
        env: dict[str, str] = {}
        secrets: list[str] = []
        for name, spec in services.items():
            env.update(spec.render_env(credentials[name]))
            secrets += spec.render_secrets(credentials[name])
        logger.info("services %s ready (request %s)", names, request_id)

        async def release() -> None:
            try:
                await self._signal(client, request_id, "release")
            finally:
                await client.aclose()

        heartbeats = asyncio.create_task(self._heartbeat_loop(client, request_id))
        return RunServices(
            env=MappingProxyType(env),
            secrets=tuple(dict.fromkeys(secrets)),
            request_id=request_id,
            _heartbeats=heartbeats,
            _release=release,
        )

    async def _answer(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        deadline: float,
        request: _Request,
    ) -> dict[str, Any]:
        """Submit and long-poll until ``ready``; ``request`` follows the request asked."""
        while True:
            request_id = request.id
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ServicesUnavailable(
                    f"no answer from the node within {self.settings.wait_seconds} seconds"
                )
            if request_id is None:
                response = await self._call(client, "POST", REQUESTS_PATH, json=body)
                if response is None:
                    continue
                if response.status_code != 202:
                    raise self._refused(response)
                answer = self._parse(response)
                request_id = request.id = str(answer["requestId"])
            else:
                wait = max(0, min(MAX_WAIT_SECONDS, int(remaining)))
                response = await self._call(
                    client, "GET", f"{REQUESTS_PATH}/{request_id}", params={"wait": wait}
                )
                if response is None:
                    continue
                if response.status_code == 404:
                    # The listener restarted and lost the request (or the
                    # answer outlived its minute): ask again.
                    logger.info("request %s is gone from the listener; asking again", request_id)
                    request.id = None
                    continue
                if response.status_code != 200:
                    raise self._refused(response)
                answer = self._parse(response)
            self._check_pair(answer, body)
            if str(answer.get("requestId")) != request_id:
                raise ServicesBlocked(REFUSED, "the listener answered for another request")
            status = answer.get("status")
            if status == "pending":
                continue
            if status == "ready":
                return answer
            request.settled = status in _FINAL
            reason = answer.get("reason")
            if status == "unavailable":
                raise ServicesUnavailable(
                    str(answer.get("message") or "the node had no budget or readiness in time")
                )
            if status == "rejected" and reason in (TEMPLATE_UNAVAILABLE, QUOTA_EXCEEDED):
                raise ServicesBlocked(
                    str(reason), self._rejection(str(reason), body, answer.get("message"))
                )
            raise ServicesBlocked(REFUSED, f"the listener answered status {status!r}")

    async def _call(
        self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
    ) -> httpx.Response | None:
        """One call; None after a pause when it is worth asking again (transport, 429, 503)."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            response = await client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("services listener: %s on %s", type(exc).__name__, method)
            await self._sleep(RETRY_SECONDS)
            return None
        if response.status_code == 429:
            await self._sleep(THROTTLED_SECONDS)
            return None
        if response.status_code == 503:
            logger.info("services listener: node_unavailable, asking again")
            await self._sleep(RETRY_SECONDS)
            return None
        return response

    def _token(self) -> str:
        """The container token, read anew for every request: the node may replace it."""
        try:
            token = self.settings.token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ServicesBlocked(
                NOT_OFFERED, f"the container token ({ENV_SERVICES_TOKEN_FILE}) is not readable"
            ) from exc
        if not token or any(c.isspace() for c in token):
            raise ServicesBlocked(
                NOT_OFFERED,
                f"the container token ({ENV_SERVICES_TOKEN_FILE}) is empty or malformed",
            )
        return token

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.url,
            transport=self._transport,
            # The long poll holds MAX_WAIT_SECONDS; the proxies of the host are
            # not the way to the listener on the network of the agents.
            timeout=httpx.Timeout(MAX_WAIT_SECONDS + 15.0),
            trust_env=False,
        )

    # -- answers ---------------------------------------------------------------

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or "requestId" not in answer:
            raise ServicesBlocked(REFUSED, "the listener answered outside its contract")
        return answer

    def _check_pair(self, answer: dict[str, Any], body: dict[str, Any]) -> None:
        if (answer.get("agentKey"), answer.get("replica")) != (body["agentKey"], body["replica"]):
            raise ServicesBlocked(REFUSED, "the listener answered for another replica")

    @staticmethod
    def _refused(response: httpx.Response) -> ServicesBlocked:
        """An error answer, by its code; the message is the listener's, never our request."""
        detail: Any = None
        with contextlib.suppress(ValueError):
            detail = (response.json() or {}).get("detail")
        code = detail.get("code") if isinstance(detail, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else None
        if response.status_code == 422 and code in (TEMPLATE_UNAVAILABLE, QUOTA_EXCEEDED):
            return ServicesBlocked(
                str(code), f"the listener refused the request: {message or code}"[:500]
            )
        return ServicesBlocked(
            REFUSED,
            f"the listener refused the request: {response.status_code} {code or ''}".strip()[:500],
        )

    @staticmethod
    def _rejection(reason: str, body: dict[str, Any], message: Any) -> str:
        asked = ", ".join(
            f"{name} ({spec['template']})" for name, spec in sorted(body["services"].items())
        )
        what = (
            "the node has no template for one of them"
            if reason == TEMPLATE_UNAVAILABLE
            else "more services than the node gives a replica"
        )
        detail = f": {message}" if message else ""
        return (
            f"{RUNNER_CONFIG_PATH} asks for {asked}; {what}{detail}. Fix the services of "
            "the repository or the templates of the node, then return the task"
        )[:500]

    @staticmethod
    def _credentials(
        answer: dict[str, Any], services: Mapping[str, ServiceSpec]
    ) -> dict[str, ServiceCredentials]:
        given = answer.get("services")
        if not isinstance(given, dict) or set(given) != set(services):
            raise ServicesBlocked(REFUSED, "the ready answer does not name the services asked for")
        credentials: dict[str, ServiceCredentials] = {}
        for name, endpoint in given.items():
            try:
                host, port = endpoint["host"], endpoint["port"]
                user, password, database = (
                    endpoint["user"],
                    endpoint["password"],
                    endpoint["database"],
                )
            except (KeyError, TypeError) as exc:
                raise ServicesBlocked(REFUSED, f"service {name}: incomplete endpoint") from exc
            if not all(isinstance(v, str) and v for v in (host, user, password, database)) or (
                isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            ):
                raise ServicesBlocked(REFUSED, f"service {name}: malformed endpoint")
            credentials[name] = ServiceCredentials(
                host=host, port=port, user=user, password=password, database=database
            )
        return credentials

    async def _reachable(self, credentials: Mapping[str, ServiceCredentials]) -> None:
        """Wait until every service accepts a TCP connection, within ``connect_seconds``."""
        deadline = self._clock() + self.connect_seconds
        for name, endpoint in sorted(credentials.items()):
            while not await self._connect(endpoint.host, endpoint.port):
                if self._clock() >= deadline:
                    raise ServicesUnavailable(
                        f"service {name} was ready but accepted no connection within "
                        f"{int(self.connect_seconds)} seconds"
                    )
                await self._sleep(1.0)

    # -- while the run lives ---------------------------------------------------

    async def _heartbeat_loop(self, client: httpx.AsyncClient, request_id: str) -> None:
        """Hold the services while the run lives; a lost hold ends the loop, not the run."""
        while True:
            # Wall time, not the injected pause of retries: the lease of the
            # node runs on the wall clock.
            await asyncio.sleep(self.heartbeat_seconds)
            status = await self._signal(client, request_id, "heartbeat")
            if status == 404:
                # The listener restarted: the hold runs out, the services stay
                # warm idleSeconds from then. The run goes on.
                logger.warning(
                    "services of request %s are no longer held (listener restarted)", request_id
                )
                return

    async def _signal(self, client: httpx.AsyncClient, request_id: str, kind: str) -> int | None:
        """A heartbeat or a release; the status it got, None when none came."""
        status: int | None = None
        # A heartbeat may wait as long as any call; a release is short.
        timeout: Any = RELEASE_TIMEOUT_SECONDS if kind == "release" else httpx.USE_CLIENT_DEFAULT
        for _ in range(SIGNAL_ATTEMPTS):
            try:
                response = await client.post(
                    f"{REQUESTS_PATH}/{request_id}/{kind}",
                    headers={"Authorization": f"Bearer {self._token()}"},
                    timeout=timeout,
                )
            except ServicesBlocked:
                logger.warning("services %s of request %s: no token", kind, request_id)
                return None
            except httpx.HTTPError as exc:
                logger.warning(
                    "services %s of request %s: %s", kind, request_id, type(exc).__name__
                )
                return None
            status = response.status_code
            if status != 429:
                break
            await self._sleep(THROTTLED_SECONDS)
        if status not in (202, 404):
            logger.warning("services %s of request %s: %s", kind, request_id, status)
        return status


async def _accepts_connection(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


class RunServicesSource:
    """What a run gets of the services its ``runner.yaml`` declares (``Agent.services``).

    ``client`` is None on a runner without a listener; ``host`` (the mode
    ``host``) hands nothing and asks nothing, whatever is declared.
    """

    def __init__(self, client: FleetServicesClient | None, *, host: bool = False) -> None:
        self.client = client
        self.host = host

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str], agent_key: str | None = None
    ) -> RunServicesSource:
        """``ValueError`` for a deployment that is neither with nor without a listener."""
        if services_mode(environ) == "host":
            return cls(None, host=True)
        settings = ServicesSettings.from_environment(environ, agent_key)
        return cls(FleetServicesClient(settings) if settings is not None else None)

    async def open(self, workspace: Workspace, *, takes_env: bool) -> RunServices:
        services = await asyncio.to_thread(declared_services, workspace)
        if not services or self.host:
            return RunServices()
        names = ", ".join(sorted(services))
        if self.client is None:
            raise ServicesBlocked(
                NOT_OFFERED,
                f"{RUNNER_CONFIG_PATH} asks for services ({names}), but this runner has no "
                f"services listener ({ENV_SERVICES_URL}): run the task on a runner whose "
                "executor kind has services: true",
            )
        if not takes_env:
            raise ServicesBlocked(
                NOT_OFFERED,
                f"{RUNNER_CONFIG_PATH} asks for services ({names}), but the executor of "
                "this runner takes no run environment",
            )
        return await self.client.acquire(services)
