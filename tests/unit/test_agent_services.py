"""The daemon's client of test services (universal-runner U013).

The fake listener below is held to the contract of the fleet, not to this
client: every request body is validated against ``ServicesRequest`` and every
answer it gives against ``ServicesAnswer``/``ServicesError`` of
``tests/fixtures/fleet_services_openapi.json`` — a verbatim copy of
``openapi/fleet-services.json`` of the fleet repository (branch
``feature/universal-runner`` at b690892; the document is generated from
``fleet_api.models`` and held to them by ``tests/test_services_contract.py``
there). Regenerate the copy after a deliberate change of the models; the
rules the JSON Schema cannot express (a ready answer carries services and no
reason, a rejected one a reason of two) are repeated in ``_answer_rules``.
"""

import asyncio
import json
import logging
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from control_plane_agent import services as svc
from control_plane_agent.runner_config import ServiceCredentials, ServiceSpec
from control_plane_agent.services import (
    FleetServicesClient,
    RunServicesSource,
    ServicesBlocked,
    ServicesSettings,
    ServicesUnavailable,
    declared_services,
    declared_services_at,
)
from control_plane_agent.workspace import Workspace

OPENAPI = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "fleet_services_openapi.json").read_text()
)
TOKEN = "fst_" + "A" * 43
PASSWORD = "p" * 32
URL_TEMPLATE = "postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator({**OPENAPI, "$ref": f"#/components/schemas/{name}"})


def _valid(name: str, document: Any) -> None:
    errors = sorted(_validator(name).iter_errors(document), key=str)
    assert not errors, f"{name}: {[e.message for e in errors]}"


def _request_errors(body: Any) -> list[str]:
    """What the listener refuses in a request body.

    The schema maps service names by ``patternProperties`` only; pydantic
    refuses a key outside the pattern, so the fake does too, with the pattern
    of the same schema.
    """
    errors = [e.message for e in _validator("ServicesRequest").iter_errors(body)]
    services = body.get("services") if isinstance(body, dict) else None
    if isinstance(services, dict):
        schema = OPENAPI["components"]["schemas"]["ServicesRequest"]["properties"]["services"]
        [pattern] = schema["patternProperties"]
        errors += [f"service name {k!r}" for k in services if not re.search(pattern, k)]
    return errors


def _answer_rules(answer: dict[str, Any]) -> None:
    """``ServicesAnswer._check`` of fleet_api.models, which JSON Schema does not carry."""
    status, services, reason = answer["status"], answer.get("services") or {}, answer.get("reason")
    if status == "ready":
        assert services and reason is None
    else:
        assert not services
    if status == "pending":
        assert reason is None
    if status == "unavailable":
        assert reason == "test_services_unavailable"
    if status == "rejected":
        assert reason in ("service_template_unavailable", "service_quota_exceeded")


class Clock:
    """Monotonic time the fake listener and the pauses of the client advance."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


Step = dict[str, Any] | int | str | Callable[[], Any]


class FakeListener:
    """``fleet-services`` by its OpenAPI: token, pair, bodies and answers checked.

    ``polls`` scripts the answers of ``GET`` one by one: ``"pending"``,
    ``"ready"``, ``"unavailable"``, ``"rejected:<reason>"``, a status code
    (``404``, ``429``, ``503``) or an exception to raise; when it runs out the
    request stays pending. ``posts`` scripts ``POST`` the same way before it
    accepts. A long poll advances the clock by its ``wait``.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        token_file: Path,
        agent_key: str = "coder",
        replica: int = 1,
        polls: list[Step] | None = None,
        posts: list[Step] | None = None,
        signals: list[int] | None = None,
        ready_host: str = "fleet-svc-db-3f9a1c0b7d2e",
        ready_port: int = 5432,
    ) -> None:
        self.clock = clock
        self.token_file = token_file
        self.pair = (agent_key, replica)
        self.polls = list(polls or [])
        self.posts = list(posts or [])
        self.signal_script = list(signals or [])
        self.ready_host = ready_host
        self.ready_port = ready_port
        self.requests: dict[str, dict[str, Any]] = {}
        self.bodies: list[dict[str, Any]] = []
        self.waits: list[int] = []
        self.signals: list[tuple[str, str]] = []
        self.tokens_seen: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- the listener ----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        self.tokens_seen.append(auth)
        if auth != f"Bearer {self.token_file.read_text().strip()}":
            return self._error(401, "invalid_token")
        parts = request.url.path.removeprefix("/api/v1/service-requests").strip("/").split("/")
        if request.method == "POST" and parts == [""]:
            return self._submit(request)
        if request.method == "GET" and len(parts) == 1:
            return self._poll(parts[0], request)
        if request.method == "POST" and len(parts) == 2 and parts[1] in ("heartbeat", "release"):
            assert request.content == b"", "signals take no body"
            return self._signal(parts[0], parts[1])
        raise AssertionError(f"not a route of the listener: {request.method} {request.url.path}")

    def _submit(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert not _request_errors(body), _request_errors(body)
        self.bodies.append(body)
        if (body["agentKey"], body["replica"]) != self.pair:
            return self._error(403, "replica_mismatch")
        step = self.posts.pop(0) if self.posts else None
        if callable(step):
            step()
        if isinstance(step, int):
            return self._error(step, {429: "too_many_requests", 503: "node_unavailable"}[step])
        if isinstance(step, str):
            return self._error(422, step)
        request_id = str(uuid.uuid4())
        self.requests[request_id] = body
        return self._answer(202, request_id, "pending")

    def _poll(self, request_id: str, request: httpx.Request) -> httpx.Response:
        wait = int(request.url.params["wait"])
        assert 0 <= wait <= svc.MAX_WAIT_SECONDS
        self.waits.append(wait)
        if request_id not in self.requests:
            return self._error(404, "request_not_found")
        step = self.polls.pop(0) if self.polls else "pending"
        if callable(step):
            step = step()
        if isinstance(step, int):
            if step == 404:
                del self.requests[request_id]
            codes = {404: "request_not_found", 429: "too_many_requests", 503: "node_unavailable"}
            return self._error(step, codes[step])
        self.clock.now += wait
        if step == "ready":
            services = {
                name: {
                    "host": self.ready_host,
                    "port": self.ready_port,
                    "user": "test",
                    "password": PASSWORD,
                    "database": "test",
                }
                for name in self.requests[request_id]["services"]
            }
            return self._answer(200, request_id, "ready", services=services)
        if step == "unavailable":
            return self._answer(200, request_id, "unavailable", reason="test_services_unavailable")
        if isinstance(step, str) and step.startswith("rejected:"):
            return self._answer(200, request_id, "rejected", reason=step.split(":", 1)[1])
        assert step == "pending", step
        return self._answer(200, request_id, "pending")

    def _signal(self, request_id: str, kind: str) -> httpx.Response:
        self.signals.append((kind, request_id))
        status = self.signal_script.pop(0) if self.signal_script else 202
        if status != 202:
            codes = {404: "request_not_found", 429: "too_many_requests", 503: "node_unavailable"}
            return self._error(status, codes[status])
        return httpx.Response(202)

    def _answer(self, code: int, request_id: str, status: str, **fields: Any) -> httpx.Response:
        answer = {
            "requestId": request_id,
            "agentKey": self.pair[0],
            "replica": self.pair[1],
            "status": status,
            **fields,
        }
        _valid("ServicesAnswer", answer)
        _answer_rules(answer)
        return httpx.Response(code, json=answer)

    @staticmethod
    def _error(status: int, code: str) -> httpx.Response:
        body = {"detail": {"code": code, "message": "refused by the fake listener"}}
        _valid("ServicesError", body)
        return httpx.Response(status, json=body)


async def _reachable(host: str, port: int) -> bool:
    return True


def _setup(
    tmp_path: Path, clock: Clock | None = None, **listener: Any
) -> tuple[FleetServicesClient, FakeListener, Clock]:
    clock = clock or Clock()
    token_file = tmp_path / "services-token"
    token_file.write_text(TOKEN + "\n")
    fake = FakeListener(clock, token_file=token_file, **listener)
    settings = ServicesSettings(
        url="http://fleet-services:8050", token_file=token_file, agent_key="coder", replica=1
    )
    client = FleetServicesClient(
        settings,
        transport=fake.transport(),
        sleep=clock.sleep,
        clock=clock,
        connect=_reachable,
        heartbeat_seconds=3600,
    )
    return client, fake, clock


DB = {"db": ServiceSpec(template="postgres-16", env={"CP_TEST_DATABASE_URL": URL_TEMPLATE})}


# -- the contract ------------------------------------------------------------------


def test_the_pinned_contract_has_the_routes_and_numbers_the_client_uses() -> None:
    paths = OPENAPI["paths"]
    base = svc.REQUESTS_PATH
    assert "post" in paths[base]
    [wait] = [p for p in paths[f"{base}/{{requestId}}"]["get"]["parameters"] if p["in"] == "query"]
    assert (wait["name"], wait["schema"]["maximum"]) == ("wait", svc.MAX_WAIT_SECONDS)
    heartbeat = paths[f"{base}/{{requestId}}/heartbeat"]["post"]
    assert f"for {svc.SERVICES_LEASE_SECONDS} seconds more" in heartbeat["description"]
    assert f"every {svc.SERVICES_HEARTBEAT_SECONDS} seconds" in heartbeat["description"]
    assert "202" in paths[f"{base}/{{requestId}}/release"]["post"]["responses"]
    request = OPENAPI["components"]["schemas"]["ServicesRequest"]["properties"]["waitSeconds"]
    assert (request["default"], request["minimum"], request["maximum"]) == (
        svc.SERVICE_WAIT_DEFAULT_SECONDS,
        svc.SERVICE_WAIT_MIN_SECONDS,
        svc.SERVICE_WAIT_MAX_SECONDS,
    )
    reasons = set(OPENAPI["components"]["schemas"]["ServicesReason"]["enum"])
    assert reasons == {svc.TEMPLATE_UNAVAILABLE, svc.QUOTA_EXCEEDED, svc.UNAVAILABLE}
    codes = set(OPENAPI["components"]["schemas"]["ServicesErrorCode"]["enum"])
    assert {svc.TEMPLATE_UNAVAILABLE, svc.QUOTA_EXCEEDED} <= codes
    statuses = set(OPENAPI["components"]["schemas"]["ServicesStatus"]["enum"])
    assert statuses == {"pending", *svc._FINAL}


def test_the_fake_listener_refuses_what_the_contract_refuses() -> None:
    """A fake that took any body would prove nothing: the schema bites."""
    good = {"agentKey": "coder", "replica": 1, "services": {"db": {"template": "postgres-16"}}}
    assert _request_errors(good) == []
    for bad in (
        {**good, "services": {"db": {"template": "postgres-16", "image": "evil"}}},
        {**good, "services": {}},
        {**good, "replica": "1"},
        {**good, "services": {"db_main": {"template": "postgres-16"}}},
        {**good, "waitSeconds": 5},
        {**good, "extra": 1},
    ):
        assert _request_errors(bad), bad


# -- ready ---------------------------------------------------------------------------


async def test_ready_services_fill_the_run_environment(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["pending", "ready"])

    run = await client.acquire(DB)

    assert dict(run.env) == {
        "CP_TEST_DATABASE_URL": (
            f"postgresql+psycopg://test:{PASSWORD}@fleet-svc-db-3f9a1c0b7d2e:5432/test"
        )
    }
    [body] = fake.bodies
    assert body == {
        "agentKey": "coder",
        "replica": 1,
        "services": {"db": {"template": "postgres-16"}},
        "waitSeconds": svc.SERVICE_WAIT_DEFAULT_SECONDS,
    }
    assert set(fake.tokens_seen) == {f"Bearer {TOKEN}"}
    assert fake.waits == [svc.MAX_WAIT_SECONDS, svc.MAX_WAIT_SECONDS]
    # The password and what carries it are the run's secrets, masked in
    # what its checks print.
    assert run.secrets == (PASSWORD, run.env["CP_TEST_DATABASE_URL"])
    # Nothing of the credentials in how the run is shown.
    assert PASSWORD not in repr(run)
    await run.close()
    assert fake.signals == [("release", run.request_id)]


async def test_several_services_each_fill_their_own_variables(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    specs = {
        "db": ServiceSpec(template="postgres-16", env={"DB_HOST": "{host}", "DB_PORT": "{port}"}),
        "graph": ServiceSpec(template="postgres-16-age", env={"GRAPH_USER": "{user}"}),
        "plain": ServiceSpec(template="redis-7", env={}),
    }

    run = await client.acquire(specs)

    assert dict(run.env) == {
        "DB_HOST": "fleet-svc-db-3f9a1c0b7d2e",
        "DB_PORT": "5432",
        "GRAPH_USER": "test",
    }
    assert set(fake.bodies[0]["services"]) == {"db", "graph", "plain"}
    # Host, port and the user name alone are no secret; each password is.
    assert set(run.secrets) == {PASSWORD}
    await run.close()


async def test_the_run_environment_is_read_only(tmp_path: Path) -> None:
    client, _, _ = _setup(tmp_path, polls=["ready"])
    run = await client.acquire(DB)
    with pytest.raises(TypeError):
        run.env["CP_TEST_DATABASE_URL"] = "elsewhere"  # type: ignore[index]
    await run.close()


# -- back to the queue -------------------------------------------------------------


async def test_unavailable_answer_sends_the_task_back_to_the_queue(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["pending", "unavailable"])

    with pytest.raises(ServicesUnavailable) as caught:
        await client.acquire(DB)

    assert caught.value.code == "test_services_unavailable"
    # The answer was final: nothing is left to release.
    assert fake.signals == []


async def test_own_timeout_withdraws_the_request(tmp_path: Path) -> None:
    """The listener never answers: the daemon gives up and releases what it asked."""
    client, fake, clock = _setup(tmp_path)  # pending forever
    start = clock.now

    with pytest.raises(ServicesUnavailable):
        await client.acquire(DB)

    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]
    waited = clock.now - start
    budget = svc.SERVICE_WAIT_DEFAULT_SECONDS + svc.LISTENER_GRACE_SECONDS
    assert budget <= waited <= budget + 2 * svc.MAX_WAIT_SECONDS


async def test_a_service_that_accepts_no_connection_is_unavailable(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    attempts: list[tuple[str, int]] = []

    async def refuse(host: str, port: int) -> bool:
        attempts.append((host, port))
        return False

    client._connect = refuse

    with pytest.raises(ServicesUnavailable) as caught:
        await client.acquire(DB)

    assert "accepted no connection" in caught.value.reason
    assert attempts and set(attempts) == {("fleet-svc-db-3f9a1c0b7d2e", 5432)}
    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]


async def test_waits_for_the_connection_before_the_run(tmp_path: Path) -> None:
    client, _, _ = _setup(tmp_path, polls=["ready"])
    attempts = iter([False, False, True])

    async def slowly(host: str, port: int) -> bool:
        return next(attempts)

    client._connect = slowly

    run = await client.acquire(DB)

    assert next(attempts, None) is None  # every attempt was needed
    await run.close()


async def test_real_connection_probe_against_a_listening_socket() -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert await svc._accepts_connection("127.0.0.1", port)
    assert not await svc._accepts_connection("127.0.0.1", port)


# -- blocked -----------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["service_template_unavailable", "service_quota_exceeded"])
async def test_rejected_answer_blocks_the_task(tmp_path: Path, reason: str) -> None:
    client, fake, _ = _setup(tmp_path, polls=[f"rejected:{reason}"])

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert caught.value.code == reason
    assert "db (postgres-16)" in caught.value.reason
    assert fake.signals == []


@pytest.mark.parametrize("code", ["service_template_unavailable", "service_quota_exceeded"])
async def test_listener_refusing_the_template_blocks_the_task(tmp_path: Path, code: str) -> None:
    client, fake, _ = _setup(tmp_path, posts=[code])

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert caught.value.code == code
    assert fake.requests == {}
    assert fake.signals == []


async def test_invalid_token_blocks_and_never_names_the_token(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path)
    fake.token_file = tmp_path / "another"
    fake.token_file.write_text("fst_" + "B" * 43)

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert caught.value.code == "test_services_refused"
    assert "401" in caught.value.reason
    assert TOKEN not in str(caught.value)


async def test_another_pair_than_the_token_is_refused(tmp_path: Path) -> None:
    client, _, _ = _setup(tmp_path, replica=2)

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert (caught.value.code, "403" in caught.value.reason) == ("test_services_refused", True)


async def test_answer_of_another_replica_is_refused_and_released(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    original = fake._answer

    def other_replica(code: int, request_id: str, status: str, **fields: Any) -> httpx.Response:
        response = original(code, request_id, status, **fields)
        if status != "ready":
            return response
        return httpx.Response(code, json={**response.json(), "replica": 7})

    fake._answer = other_replica  # type: ignore[method-assign]

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert caught.value.code == "test_services_refused"
    [request_id] = fake.requests
    assert fake.signals == [("release", request_id)]


async def test_ready_answer_without_a_requested_service_is_refused(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    original = fake._answer

    def missing(code: int, request_id: str, status: str, **fields: Any) -> httpx.Response:
        response = original(code, request_id, status, **fields)
        if status == "ready":
            answer = response.json()
            answer["services"] = {"other": answer["services"]["db"]}
            return httpx.Response(code, json=answer)
        return response

    fake._answer = missing  # type: ignore[method-assign]

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert caught.value.code == "test_services_refused"
    assert fake.signals and fake.signals[0][0] == "release"


@pytest.mark.parametrize(
    "endpoint",
    [
        {"host": "h", "port": "5432", "user": "u", "password": PASSWORD, "database": "d"},
        {"host": "h", "port": True, "user": "u", "password": PASSWORD, "database": "d"},
        {"host": "h", "port": 0, "user": "u", "password": PASSWORD, "database": "d"},
        {"host": "", "port": 1, "user": "u", "password": PASSWORD, "database": "d"},
        {"host": "h", "port": 1, "user": "u", "password": None, "database": "d"},
        {"host": "h", "port": 1, "user": "u", "database": "d"},
        None,
    ],
)
def test_malformed_endpoint_is_refused(endpoint: Any) -> None:
    with pytest.raises(ServicesBlocked) as caught:
        FleetServicesClient._credentials({"services": {"db": endpoint}}, DB)
    assert caught.value.code == "test_services_refused"
    assert PASSWORD not in str(caught.value)


@pytest.mark.parametrize("body", [b"not json", b"[]", b"null", b'{"status": "pending"}'])
async def test_answer_outside_the_contract_is_refused(tmp_path: Path, body: bytes) -> None:
    client, _, _ = _setup(tmp_path)
    client._transport = httpx.MockTransport(lambda request: httpx.Response(202, content=body))

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert caught.value.code == "test_services_refused"


@pytest.mark.parametrize("content", [None, "", "  \n", "fst_a b"])
async def test_unreadable_token_blocks_before_any_request(
    tmp_path: Path, content: str | None
) -> None:
    client, fake, _ = _setup(tmp_path)
    if content is None:
        client.settings.token_file.unlink()
    else:
        client.settings.token_file.write_text(content)

    with pytest.raises(ServicesBlocked) as caught:
        await client.acquire(DB)

    assert caught.value.code == "test_services_not_offered"
    assert str(tmp_path) not in str(caught.value)
    assert fake.bodies == []


# -- the listener comes and goes -----------------------------------------------------


async def test_lost_request_is_asked_again(tmp_path: Path) -> None:
    """404 on a poll: the listener restarted and lost it — a new request, then ready."""
    client, fake, _ = _setup(tmp_path, polls=["pending", 404, "ready"])

    run = await client.acquire(DB)

    assert len(fake.bodies) == 2
    assert run.request_id in fake.requests
    await run.close()


@pytest.mark.parametrize("status", [429, 503])
async def test_throttled_or_node_unavailable_is_asked_again(tmp_path: Path, status: int) -> None:
    client, fake, clock = _setup(tmp_path, posts=[status, status], polls=[status, "ready"])

    run = await client.acquire(DB)

    assert len(fake.bodies) == 3
    pause = svc.THROTTLED_SECONDS if status == 429 else svc.RETRY_SECONDS
    assert clock.pauses == [pause] * 3
    await run.close()


async def test_transport_error_is_asked_again(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    failures = iter([httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
    handle = fake.handle

    def flaky(request: httpx.Request) -> httpx.Response:
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return handle(request)

    client._transport = httpx.MockTransport(flaky)

    run = await client.acquire(DB)

    assert len(fake.bodies) == 1
    await run.close()


async def test_token_is_read_for_every_request(tmp_path: Path) -> None:
    """The node replaces the token file of a recreated container: no stale copy is kept."""
    rotated = "fst_" + "C" * 43
    clock = Clock()
    client, fake, _ = _setup(tmp_path, clock, polls=["pending", "ready"])

    def rotate() -> str:
        client.settings.token_file.write_text(rotated)
        fake.token_file.write_text(rotated)
        return "pending"

    fake.polls.insert(0, rotate)

    run = await client.acquire(DB)
    await run.close()

    assert fake.tokens_seen[0] == f"Bearer {TOKEN}"
    assert fake.tokens_seen[-1] == f"Bearer {rotated}"


# -- while the run lives -------------------------------------------------------------


async def test_heartbeats_hold_the_services_until_release(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    client.heartbeat_seconds = 0.01

    run = await client.acquire(DB)
    for _ in range(200):
        if len(fake.signals) >= 2:
            break
        await asyncio.sleep(0.01)
    await run.close()

    kinds = [kind for kind, _ in fake.signals]
    assert kinds[:2] == ["heartbeat", "heartbeat"]
    assert kinds[-1] == "release" and kinds.count("release") == 1
    assert {request_id for _, request_id in fake.signals} == {run.request_id}


async def test_lost_hold_stops_heartbeats_and_the_run_goes_on(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"], signals=[404])
    client.heartbeat_seconds = 0.01

    run = await client.acquire(DB)
    for _ in range(200):
        if run._heartbeats is not None and run._heartbeats.done():
            break
        await asyncio.sleep(0.01)
    assert run._heartbeats is not None and run._heartbeats.done()
    assert run._heartbeats.exception() is None
    await asyncio.sleep(0.05)
    await run.close()

    assert [kind for kind, _ in fake.signals] == ["heartbeat", "release"]


async def test_throttled_signal_is_sent_again_after_a_second(tmp_path: Path) -> None:
    client, fake, clock = _setup(tmp_path, polls=["ready"], signals=[429])

    run = await client.acquire(DB)
    await run.close()

    assert [kind for kind, _ in fake.signals] == ["release", "release"]
    assert clock.pauses == [svc.THROTTLED_SECONDS]


async def test_release_failure_does_not_fail_the_run(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"], signals=[503])

    run = await client.acquire(DB)
    await run.close()

    assert [kind for kind, _ in fake.signals] == ["release"]


async def test_close_is_idempotent(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])

    run = await client.acquire(DB)
    await run.close()
    await run.close()

    assert [kind for kind, _ in fake.signals] == ["release"]


async def test_close_releases_after_heartbeats_that_broke(tmp_path: Path) -> None:
    """A heartbeat loop that died of anything but cancellation does not stop the release."""
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    client.heartbeat_seconds = 0.01
    handle = fake.handle

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/heartbeat"):
            raise RuntimeError("not a transport error")
        return handle(request)

    client._transport = httpx.MockTransport(broken)
    made: list[httpx.AsyncClient] = []
    http = client._http

    def recorded() -> httpx.AsyncClient:
        made.append(http())
        return made[-1]

    client._http = recorded  # type: ignore[method-assign]
    run = await client.acquire(DB)
    heartbeats = run._heartbeats
    assert heartbeats is not None
    for _ in range(200):
        if heartbeats.done():
            break
        await asyncio.sleep(0.01)
    assert isinstance(heartbeats.exception(), RuntimeError)

    await run.close()  # does not raise
    await run.close()

    assert fake.signals == [("release", run.request_id)]
    [http_client] = made
    assert http_client.is_closed


async def test_close_releases_when_the_heartbeats_are_cancelled_from_outside(
    tmp_path: Path,
) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    run = await client.acquire(DB)
    assert run._heartbeats is not None
    run._heartbeats.cancel()
    await asyncio.sleep(0)

    await run.close()

    assert fake.signals == [("release", run.request_id)]


async def test_release_has_a_short_timeout_and_heartbeats_the_usual_one(tmp_path: Path) -> None:
    """A listener that hangs holds the run's copy for seconds, not a long poll."""
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    client.heartbeat_seconds = 0.01
    handle = fake.handle
    timeouts: dict[str, float] = {}

    def timed(request: httpx.Request) -> httpx.Response:
        kind = request.url.path.rsplit("/", 1)[-1]
        timeouts.setdefault(kind, request.extensions["timeout"]["read"])
        return handle(request)

    client._transport = httpx.MockTransport(timed)
    run = await client.acquire(DB)
    for _ in range(200):
        if "heartbeat" in timeouts:
            break
        await asyncio.sleep(0.01)
    await run.close()

    assert timeouts["release"] == svc.RELEASE_TIMEOUT_SECONDS == 10.0
    assert timeouts["heartbeat"] == svc.MAX_WAIT_SECONDS + 15.0


async def test_release_that_times_out_does_not_fail_the_run(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready"])
    handle = fake.handle

    def hanging(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/release"):
            raise httpx.ReadTimeout("no answer", request=request)
        return handle(request)

    client._transport = httpx.MockTransport(hanging)
    run = await client.acquire(DB)

    await run.close()


async def test_cancelled_wait_withdraws_the_request(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path)  # pending forever
    started = asyncio.Event()
    handle = fake.handle

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            started.set()
            await asyncio.Event().wait()  # a long poll that never ends
        return handle(request)

    client._transport = httpx.MockTransport(slow)
    task = asyncio.create_task(client.acquire(DB))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [request_id] = fake.requests
    assert ("release", request_id) in fake.signals


async def test_two_runs_at_once_hold_their_own_requests(tmp_path: Path) -> None:
    client, fake, _ = _setup(tmp_path, polls=["ready", "ready"])

    first, second = await asyncio.gather(client.acquire(DB), client.acquire(DB))
    await first.close()
    await second.close()

    assert first.request_id != second.request_id
    assert sorted(fake.signals) == sorted(
        [("release", first.request_id), ("release", second.request_id)]
    )


async def test_nothing_secret_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Alembic's fileConfig in the migration tests disables loggers that exist by then.
    monkeypatch.setattr(logging.getLogger("control_plane_agent.services"), "disabled", False)
    caplog.set_level(logging.DEBUG)
    client, _, _ = _setup(tmp_path, polls=[503, "ready"], signals=[503])

    run = await client.acquire(DB)
    await run.close()

    assert caplog.records
    assert TOKEN not in caplog.text and PASSWORD not in caplog.text


# -- settings ----------------------------------------------------------------------


def test_settings_come_from_the_container_environment(tmp_path: Path) -> None:
    environ = {
        "FLEET_SERVICES_URL": "http://fleet-services:8050/",
        "FLEET_SERVICES_TOKEN_FILE": "/run/fleet/services-token",
        "FLEET_REPLICA": "1",
        "CONTROL_PLANE_AGENT_KEY": "coder",
    }
    settings = ServicesSettings.from_environment(environ)
    assert settings is not None
    assert (settings.url, settings.agent_key, settings.replica) == (
        "http://fleet-services:8050",
        "coder",
        1,
    )
    assert settings.token_file == Path("/run/fleet/services-token")
    # The key of the revision wins over the variable.
    renamed = ServicesSettings.from_environment(environ, "coder-2")
    assert renamed is not None and renamed.agent_key == "coder-2"


def test_without_a_listener_there_are_no_settings() -> None:
    assert ServicesSettings.from_environment({}) is None
    assert ServicesSettings.from_environment({"FLEET_SERVICES_URL": " "}) is None


LISTENER = {"FLEET_SERVICES_URL": "http://l", "FLEET_SERVICES_TOKEN_FILE": "/t"}


@pytest.mark.parametrize(
    "environ",
    [
        {"FLEET_SERVICES_URL": "http://l:8050"},
        {"FLEET_SERVICES_TOKEN_FILE": "/run/fleet/services-token"},
        LISTENER,
        {**LISTENER, "FLEET_REPLICA": ""},
        {**LISTENER, "FLEET_REPLICA": "-1"},
        {**LISTENER, "FLEET_REPLICA": "100"},
        {**LISTENER, "FLEET_REPLICA": "1.0"},
        # A digit to str.isdigit, not to the contract (ARABIC-INDIC DIGIT ONE).
        {**LISTENER, "FLEET_REPLICA": "\u0661"},
        {**LISTENER, "FLEET_SERVICES_URL": "ftp://l", "FLEET_REPLICA": "1"},
        # No agent key: neither the revision nor the variable names one.
        {**LISTENER, "FLEET_REPLICA": "1"},
    ],
)
def test_half_configured_listener_is_a_broken_deployment(environ: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ServicesSettings.from_environment({"CONTROL_PLANE_AGENT_KEY": "", **environ})


def test_replica_must_be_an_int_not_a_bool() -> None:
    with pytest.raises(ValueError):
        ServicesSettings(url="http://l", token_file=Path("/t"), agent_key="coder", replica=True)


@pytest.mark.parametrize(
    ("value", "expected"), [(None, "fleet"), ("", "fleet"), ("fleet", "fleet"), ("host", "host")]
)
def test_services_mode(value: str | None, expected: str) -> None:
    environ = {} if value is None else {"CONTROL_PLANE_AGENT_SERVICES": value}
    assert svc.services_mode(environ) == expected


@pytest.mark.parametrize("value", ["off", "HOST", "static"])
def test_unknown_services_mode_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        svc.services_mode({"CONTROL_PLANE_AGENT_SERVICES": value})
    with pytest.raises(ValueError):
        RunServicesSource.from_environment({"CONTROL_PLANE_AGENT_SERVICES": value})


def test_source_from_environment() -> None:
    assert RunServicesSource.from_environment({}).client is None
    host = RunServicesSource.from_environment(
        {"CONTROL_PLANE_AGENT_SERVICES": "host", "FLEET_SERVICES_URL": "http://l"}
    )
    assert (host.client, host.host) == (None, True)
    fleet = RunServicesSource.from_environment(
        {
            "FLEET_SERVICES_URL": "http://l",
            "FLEET_SERVICES_TOKEN_FILE": "/t",
            "FLEET_REPLICA": "0",
        },
        "coder",
    )
    assert fleet.client is not None and fleet.client.settings.replica == 0


# -- runner.yaml of the base -------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


RUNNER_YAML = """\
version: 1
services:
  db:
    template: postgres-16
    env:
      CP_TEST_DATABASE_URL: "postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"
"""


def _copy(tmp_path: Path, runner_yaml: str | None) -> Workspace:
    path = tmp_path / "copy"
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n")
    if runner_yaml is not None:
        (path / ".agents").mkdir()
        (path / ".agents" / "runner.yaml").write_text(runner_yaml)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    base = _git(path, "rev-parse", "HEAD")
    return Workspace(
        key="TASK-1",
        branch="main",
        path=path,
        base_commit=base,
        reused=False,
        base_revision=base,
    )


def test_services_are_read_from_the_base_not_from_the_task_branch(tmp_path: Path) -> None:
    workspace = _copy(tmp_path, RUNNER_YAML)
    # The task changes runner.yaml on its branch: it does not count yet.
    (workspace.path / ".agents" / "runner.yaml").write_text(
        RUNNER_YAML.replace("postgres-16", "postgres-17")
    )
    _git(workspace.path, "commit", "-qam", "task: another template")
    workspace.base_commit = _git(workspace.path, "rev-parse", "HEAD")

    services = declared_services(workspace)

    assert {n: s.template for n, s in services.items()} == {"db": "postgres-16"}


@pytest.mark.parametrize("runner_yaml", [None, "version: 1\n", "version: 1\nservices: {}\n"])
def test_no_services_declared(tmp_path: Path, runner_yaml: str | None) -> None:
    assert dict(declared_services(_copy(tmp_path, runner_yaml))) == {}


@pytest.mark.parametrize(
    "runner_yaml",
    [
        "version: 2\n",
        "version: 1\nservices:\n  db: {}\n",
        "version: 1\nservices:\n  db: {template: pg, env: {X: '{name}'}}\n",
        "version: 1\nservices: [db]\n",
        ": not yaml\n",
    ],
)
def test_invalid_runner_yaml_blocks(tmp_path: Path, runner_yaml: str) -> None:
    with pytest.raises(ServicesBlocked) as caught:
        declared_services(_copy(tmp_path, runner_yaml))
    assert caught.value.code == "runner_config_invalid"
    assert str(tmp_path) not in caught.value.reason


def test_services_at_a_revision_of_a_repository(tmp_path: Path) -> None:
    workspace = _copy(tmp_path, RUNNER_YAML)
    assert set(declared_services_at(workspace.path, "HEAD")) == {"db"}
    assert declared_services_at(workspace.path, "no-such-revision") == {}


def test_no_services_outside_a_repository(tmp_path: Path) -> None:
    assert declared_services_at(tmp_path, "HEAD") == {}


class _Acquiring:
    def __init__(self) -> None:
        self.asked: list[dict[str, str]] = []

    async def acquire(self, services: Any) -> svc.RunServices:
        self.asked.append({n: s.template for n, s in services.items()})
        return svc.RunServices(env={"X": "1"}, request_id="r")


async def test_source_asks_for_the_declared_services(tmp_path: Path) -> None:
    fleet = _Acquiring()
    source = RunServicesSource(fleet)  # type: ignore[arg-type]

    run = await source.open(_copy(tmp_path, RUNNER_YAML), takes_env=True)

    assert fleet.asked == [{"db": "postgres-16"}]
    assert dict(run.env) == {"X": "1"}


async def test_source_without_declared_services_asks_nothing(tmp_path: Path) -> None:
    fleet = _Acquiring()
    for source in (RunServicesSource(fleet), RunServicesSource(None)):  # type: ignore[arg-type]
        run = await source.open(_copy(tmp_path / str(id(source)), None), takes_env=False)
        assert dict(run.env) == {}
    assert fleet.asked == []


async def test_host_mode_asks_nothing(tmp_path: Path) -> None:
    run = await RunServicesSource(None, host=True).open(
        _copy(tmp_path, RUNNER_YAML), takes_env=True
    )
    assert (dict(run.env), run.request_id) == ({}, None)


async def test_declared_services_without_a_listener_block(tmp_path: Path) -> None:
    with pytest.raises(ServicesBlocked) as caught:
        await RunServicesSource(None).open(_copy(tmp_path, RUNNER_YAML), takes_env=True)
    assert caught.value.code == "test_services_not_offered"
    assert "FLEET_SERVICES_URL" in caught.value.reason and "db" in caught.value.reason


async def test_declared_services_with_an_executor_without_env_block(tmp_path: Path) -> None:
    fleet = _Acquiring()
    with pytest.raises(ServicesBlocked) as caught:
        await RunServicesSource(fleet).open(  # type: ignore[arg-type]
            _copy(tmp_path, RUNNER_YAML), takes_env=False
        )
    assert caught.value.code == "test_services_not_offered"
    assert fleet.asked == []


def test_the_secrets_of_a_service_are_its_password_and_what_carries_it() -> None:
    spec = ServiceSpec(
        template="postgres-16",
        env={
            "URL": URL_TEMPLATE,
            "HOST": "{host}:{port}",
            "NAME": "{database}",
            "WHO": "{user}",
            "LITERAL": "{{password}}",
            "PLAIN": "control_plane",
        },
    )
    credentials = ServiceCredentials(
        host="db", port=5432, user="tester", password=PASSWORD, database="cp"
    )
    assert spec.render_secrets(credentials) == (
        PASSWORD,
        f"postgresql+psycopg://tester:{PASSWORD}@db:5432/cp",
    )
    assert ServiceSpec(template="redis-7", env={}).render_secrets(credentials) == (PASSWORD,)


@pytest.mark.parametrize("template", ["{user}", " {user} "])
def test_the_user_alone_is_no_secret(template: str) -> None:
    # The user of a template (postgres) masked would turn every "postgres" of
    # the output into ***; the password stays hidden.
    spec = ServiceSpec(template="postgres-16", env={"PGUSER": template, "PGPASSWORD": "{password}"})
    credentials = ServiceCredentials(
        host="db", port=5432, user="postgres", password=PASSWORD, database="cp"
    )
    assert spec.render_secrets(credentials) == (PASSWORD,)


@pytest.mark.parametrize(
    ("template", "shown"),
    [
        ("{user}:{password}", f"postgres:{PASSWORD}"),
        ("user={user}", "user=postgres"),
        ("{user}@{host}", "postgres@db"),
    ],
)
def test_the_user_with_anything_else_is_a_secret(template: str, shown: str) -> None:
    spec = ServiceSpec(template="postgres-16", env={"V": template})
    credentials = ServiceCredentials(
        host="db", port=5432, user="postgres", password=PASSWORD, database="cp"
    )
    assert spec.render_secrets(credentials) == (PASSWORD, shown)


def test_an_empty_password_is_no_secret() -> None:
    spec = ServiceSpec(template="postgres-16", env={"PW": "{password}"})
    credentials = ServiceCredentials(host="db", port=5432, user="u", password="", database="d")
    assert spec.render_secrets(credentials) == ()


def test_no_services_no_secrets() -> None:
    assert svc.RunServices().secrets == ()
