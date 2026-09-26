"""Working-context API: operational truth + degraded memory semantics."""

from typing import Any

import httpx
import pytest

from control_plane.infrastructure.context_provider.base import (
    ContextProviderError,
    IngestResult,
)
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)


class SpyProvider:
    """Fake provider that records every request it receives."""

    def __init__(self, *, fail: str | None = None) -> None:
        # None | "error" | "timeout" | "multi" | "multi-invalid" | "multi-forbidden"
        self.fail = fail
        self.context_requests: list[dict[str, Any]] = []
        self.ingest_requests: list[tuple[str, list[dict[str, Any]]]] = []

    async def build_context(
        self,
        *,
        namespace: str,
        request: dict[str, Any],
        trace_run_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        self.context_requests.append(
            {"namespace": namespace, "namespaces": namespaces or [namespace], **request}
        )
        if self.fail == "error":
            raise ContextProviderError("boom", retryable=True)
        if self.fail == "multi" and namespaces and len(namespaces) > 1:
            raise ContextProviderError("extra field", retryable=False, status=422)
        if self.fail == "multi-invalid" and namespaces and len(namespaces) > 1:
            raise ContextProviderError("bad request", retryable=False, status=400)
        if self.fail == "multi-forbidden" and namespaces and len(namespaces) > 1:
            raise ContextProviderError("namespace denied", retryable=True, status=403)
        if self.fail == "timeout":
            import asyncio

            await asyncio.sleep(3600)
        return {
            "query": request.get("query", ""),
            "sections": [
                {
                    "kind": "relevant_facts",
                    "items": [
                        {
                            "kind": "fact",
                            "id": "fact-1",
                            "text": "Agent A owns task (STALE remembered fact)",
                            "score": 0.9,
                        }
                    ],
                }
            ],
            "sources": [],
            "token_estimate": 100,
            "budget": {"max_tokens": request.get("max_tokens", 8000), "dropped": []},
            "trace_id": "ctx-test123",
        }

    async def ingest_batch(
        self,
        *,
        namespace: str,
        observations: list[dict[str, Any]],
        trace_run_id: str | None = None,
    ) -> IngestResult:
        self.ingest_requests.append((namespace, observations))
        return IngestResult(accepted=len(observations))

    async def healthy(self) -> bool:
        return self.fail is None

    async def aclose(self) -> None:
        pass


async def test_context_without_provider_is_operational_only(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    response = await client.post("/api/v1/context", json={}, headers=auth(agent_key))
    assert response.status_code == 200
    body = response.json()
    assert body["memoryStatus"] == "disabled"
    assert body["memory"] is None
    assert body["operational"]["principal"]["id"]
    assert body["freshness"]["currentCursor"].startswith("ec1_")
    assert "context provider is not configured" in body["warnings"][0]


async def test_context_combines_operational_and_memory(client: httpx.AsyncClient, app) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="Fix the race")
    session = await open_session(client, agent_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()

    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        response = await client.post(
            "/api/v1/context",
            json={"task": task["id"], "query": "continue work"},
            headers=auth(agent_key),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200
    body = response.json()

    # Authoritative current state, from THIS transaction.
    assert body["operational"]["focus"]["task"]["id"] == task["id"]
    assert body["operational"]["activeClaims"][0]["id"] == claim["id"]
    # Durable memory, clearly separated, with provenance/trace.
    assert body["memoryStatus"] == "ok"
    assert body["memoryTraceId"] == "ctx-test123"
    stale_fact = body["memory"]["sections"][0]["items"][0]["text"]
    assert "STALE" in stale_fact  # memory is visible…
    # …but the claim truth lives ONLY in operational (memory can't override).
    assert body["operational"]["activeClaims"][0]["sessionId"] == session["id"]

    # The provider saw resolved, tenant-authorized scopes and ephemeral state.
    request = spy.context_requests[0]
    assert request["namespace"] == f"tenant:{boot['tenant']['id']}"
    assert f"task:{task['id']}" in request["scopes"]
    assert request["subject"] == {"type": "task", "id": task["id"]}
    assert request["ephemeral_context"]["task"]["id"] == task["id"]
    assert request["ephemeral_context"]["activeClaims"][0]["id"] == claim["id"]


async def test_context_degrades_on_provider_error(client: httpx.AsyncClient, app) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    app.state.context_provider = SpyProvider(fail="error")
    try:
        response = await client.post("/api/v1/context", json={}, headers=auth(agent_key))
    finally:
        app.state.context_provider = None
    assert response.status_code == 200  # never a 5xx while operational works
    body = response.json()
    assert body["memoryStatus"] == "unavailable"
    assert body["memory"] is None
    assert body["operational"]["principal"]["id"]
    assert any("unavailable" in w for w in body["warnings"])


async def test_context_times_out_to_degraded(client: httpx.AsyncClient, app, settings) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    settings.context_timeout_seconds = 0.05
    app.state.context_provider = SpyProvider(fail="timeout")
    try:
        response = await client.post("/api/v1/context", json={}, headers=auth(agent_key))
    finally:
        app.state.context_provider = None
        settings.context_timeout_seconds = 3.0
    body = response.json()
    assert body["memoryStatus"] == "timeout"
    assert body["operational"]["principal"]["id"]


async def test_context_scope_cannot_cross_tenants(
    client: httpx.AsyncClient, app, sync_engine
) -> None:
    """A crafted workspace/task reference from another tenant is rejected
    BEFORE any provider call — the spy must never see it."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    _tenant, other_key = make_tenant_directly(sync_engine, "victim")
    foreign_task = await create_task(client, other_key, title="Secret")

    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        by_task = await client.post(
            "/api/v1/context", json={"task": foreign_task["id"]}, headers=auth(agent_key)
        )
        by_workspace = await client.post(
            "/api/v1/context",
            json={"workspaceId": "00000000-0000-0000-0000-000000000001"},
            headers=auth(agent_key),
        )
    finally:
        app.state.context_provider = None
    assert by_task.status_code == 404
    assert by_workspace.status_code == 404
    assert spy.context_requests == []  # upstream never contacted


async def test_context_token_budget_is_clamped(client: httpx.AsyncClient, app, settings) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        await client.post("/api/v1/context", json={"maxTokens": 999_999}, headers=auth(agent_key))
    finally:
        app.state.context_provider = None
    assert spy.context_requests[0]["max_tokens"] == settings.context_max_tokens_limit


async def test_context_reports_memory_freshness(client: httpx.AsyncClient, sync_engine) -> None:
    """With an adapter cursor present, the response shows both cursors and
    the per-tenant lag."""
    from sqlalchemy import text

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_consumer_cursors (name, tenant_id, tx_id, sequence) "
                "VALUES ('context-adapter', :tenant, 0, 0)"
            ),
            {"tenant": boot["tenant"]["id"]},
        )
    await create_task(client, admin_key, title="Not yet in memory")

    response = await client.post("/api/v1/context", json={}, headers=auth(agent_key))
    freshness = response.json()["freshness"]
    assert freshness["currentCursor"].startswith("ec1_")
    assert freshness["memoryCursor"].startswith("ec1_")
    assert freshness["memoryLagEvents"] > 0


async def test_workspace_scope_includes_authorized_ancestors(
    client: httpx.AsyncClient, app
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    root = await create_workspace(client, admin_key, "org")
    child = await create_workspace(client, admin_key, "backend", parent_id=root["id"])
    task = await create_task(client, admin_key, workspaceId=child["id"])

    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        await client.post("/api/v1/context", json={"task": task["id"]}, headers=auth(agent_key))
    finally:
        app.state.context_provider = None
    scopes = spy.context_requests[0]["scopes"]
    assert f"workspace:{child['id']}" in scopes
    assert f"workspace:{root['id']}" in scopes


async def test_context_focus_requires_tasks_read(client: httpx.AsyncClient) -> None:
    """The working-context door must not bypass the permission model."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, limited_key = await create_agent_with_key(client, admin_key, permissions=["sessions.open"])
    task = await create_task(client, admin_key, title="Gated")

    # Focused read without tasks.read -> 403, same as GET /tasks/{id}.
    response = await client.post(
        "/api/v1/context", json={"task": task["id"]}, headers=auth(limited_key)
    )
    assert response.status_code == 403

    # Unfocused call still works (self-data only)…
    response = await client.post("/api/v1/context", json={}, headers=auth(limited_key))
    assert response.status_code == 200


async def test_context_memory_requires_events_read(client: httpx.AsyncClient, app) -> None:
    """Memory derives from the journal: recall is gated like journal reads,
    but degrades instead of 403 (the operational half is the caller's own)."""
    boot = await do_bootstrap(client)
    _, limited_key = await create_agent_with_key(
        client, boot["apiKey"]["key"], permissions=["sessions.open", "tasks.read"]
    )
    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        response = await client.post("/api/v1/context", json={}, headers=auth(limited_key))
    finally:
        app.state.context_provider = None
    body = response.json()
    assert response.status_code == 200
    assert body["memoryStatus"] == "forbidden"
    assert spy.context_requests == []  # provider never contacted


async def test_context_artifacts_block_respects_artifacts_read(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, no_artifacts_key = await create_agent_with_key(
        client, admin_key, permissions=["sessions.open", "tasks.read", "events.read"]
    )
    task = await create_task(client, admin_key, title="With artifact")
    await client.post(
        "/api/v1/artifacts",
        json={"type": "report", "name": "secret.md", "task": task["id"]},
        headers=auth(admin_key),
    )
    body = (
        await client.post(
            "/api/v1/context", json={"task": task["id"]}, headers=auth(no_artifacts_key)
        )
    ).json()
    assert body["operational"]["focus"]["artifacts"] == []


async def test_task_context_query_carries_description_and_workspace_namespace(
    client: httpx.AsyncClient, app
) -> None:
    """CP-ADR-0059: retrieval sees the task description, and the read spans the
    tenant namespace plus the namespace of the task's top-level workspace."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    root = await create_workspace(client, admin_key, "org")
    child = await create_workspace(client, admin_key, "backend", parent_id=root["id"])
    task = await create_task(
        client,
        admin_key,
        title="Fix the race",
        description="The journal inverts xid and sequence under load.",
        workspaceId=child["id"],
    )

    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        response = await client.post(
            "/api/v1/context", json={"task": task["id"]}, headers=auth(agent_key)
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200
    request = spy.context_requests[0]
    assert request["query"].startswith("Fix the race")
    assert "inverts xid and sequence" in request["query"]
    assert request["namespace"] == f"tenant:{tenant_id}"
    assert request["namespaces"] == [f"tenant:{tenant_id}", f"tenant:{tenant_id}:ws:{root['id']}"]
    # Point-in-time recall is opt-in: absent unless the caller asks for it.
    assert "as_of" not in request


async def test_explicit_query_wins_and_task_without_workspace_reads_tenant_only(
    client: httpx.AsyncClient, app
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="T", description="D")

    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        await client.post(
            "/api/v1/context",
            json={"task": task["id"], "query": "what broke?"},
            headers=auth(agent_key),
        )
    finally:
        app.state.context_provider = None
    request = spy.context_requests[0]
    assert request["query"] == "what broke?"
    assert task["workspaceId"] is None
    assert request["namespaces"] == [f"tenant:{boot['tenant']['id']}"]
    assert "allowedScopes" not in request


async def test_as_of_is_passed_through_when_given(client: httpx.AsyncClient, app) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        ok = await client.post(
            "/api/v1/context",
            json={"asOf": "2026-09-01T12:00:00+00:00"},
            headers=auth(agent_key),
        )
        naive = await client.post(
            "/api/v1/context", json={"asOf": "2026-09-01T12:00:00"}, headers=auth(agent_key)
        )
    finally:
        app.state.context_provider = None
    assert ok.status_code == 200
    assert spy.context_requests[0]["as_of"] == "2026-09-01T12:00:00+00:00"
    # A moment without a timezone is ambiguous: rejected, never guessed.
    assert naive.status_code == 400


@pytest.mark.parametrize("fail", ["multi", "multi-invalid", "multi-forbidden"])
async def test_memory_rejecting_several_namespaces_degrades_to_tenant_only(
    client: httpx.AsyncClient, app, fail: str
) -> None:
    """Invalid (400/422) or forbidden (403) workspace namespace: the tenant
    namespace is still read, and still narrowed. The tenant namespace holds
    journal observations tagged workspace:<id> of every subtree, so a retry
    without allowedScopes would hand the task its siblings' events."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    root = await create_workspace(client, admin_key, "org")
    task = await create_task(client, admin_key, workspaceId=root["id"])

    spy = SpyProvider(fail=fail)
    app.state.context_provider = spy
    try:
        response = await client.post(
            "/api/v1/context", json={"task": task["id"]}, headers=auth(agent_key)
        )
    finally:
        app.state.context_provider = None
    body = response.json()
    assert body["memoryStatus"] == "ok"
    assert any("tenant namespace only" in w for w in body["warnings"])
    assert [len(r["namespaces"]) for r in spy.context_requests] == [2, 1]
    first, retry = spy.context_requests
    assert first["allowedScopes"]
    assert retry["allowedScopes"] == first["allowedScopes"]
    assert f"workspace:{root['id']}" in retry["allowedScopes"]


async def test_workspace_namespace_is_read_only_when_visible(
    client: httpx.AsyncClient, app, monkeypatch
) -> None:
    """Policy mode: a workspace namespace outside the PDP-visible set is not asked for."""
    from control_plane.application.queries import context as context_query

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    visible_ws = await create_workspace(client, admin_key, "open")
    hidden_ws = await create_workspace(client, admin_key, "closed")
    visible_task = await create_task(client, admin_key, workspaceId=visible_ws["id"])
    hidden_task = await create_task(client, admin_key, workspaceId=hidden_ws["id"])

    async def visible_objects(ctx: Any, action: str, object_type: str) -> set[str]:
        if object_type == "memory_namespace":
            return {f"tenant-{tenant_id}", f"ws-{visible_ws['id']}"}
        return {visible_ws["id"]}

    monkeypatch.setattr(context_query, "visible_objects", visible_objects)
    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        for task in (visible_task, hidden_task):
            await client.post("/api/v1/context", json={"task": task["id"]}, headers=auth(agent_key))
    finally:
        app.state.context_provider = None
    ns = f"tenant:{tenant_id}"
    assert spy.context_requests[0]["namespaces"] == [ns, f"{ns}:ws:{visible_ws['id']}"]
    assert spy.context_requests[1]["namespaces"] == [ns]


async def test_local_mode_workspace_read_request_is_narrowed_to_focus_and_ancestors(
    client: httpx.AsyncClient, app
) -> None:
    """Local mode: the root workspace namespace holds the whole subtree, so the
    request is narrowed to the focus workspace and its ancestors (CP-ADR-0059).

    This checks the request body only. That Memory honours the narrowing (the
    client allowedScopes intersected with server visibility, a sibling subtree
    not returned) is the memory-service contract MEM-ADR-021 and is tested
    there (tests/integration/test_visibility_narrowing.py)."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    root = await create_workspace(client, admin_key, "org")
    backend = await create_workspace(client, admin_key, "backend", parent_id=root["id"])
    service = await create_workspace(client, admin_key, "api", parent_id=backend["id"])
    sibling = await create_workspace(client, admin_key, "finance", parent_id=root["id"])
    task = await create_task(client, admin_key, workspaceId=service["id"])

    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        await client.post("/api/v1/context", json={"task": task["id"]}, headers=auth(agent_key))
    finally:
        app.state.context_provider = None
    request = spy.context_requests[0]
    assert request["namespaces"] == [f"tenant:{tenant_id}", f"tenant:{tenant_id}:ws:{root['id']}"]
    allowed = request["allowedScopes"]
    assert allowed[:3] == [
        f"workspace:{service['id']}",
        f"workspace:{backend['id']}",
        f"workspace:{root['id']}",
    ]
    assert f"workspace:{sibling['id']}" not in allowed
    assert f"principal:{agent['id']}" in allowed[3:]
    assert all(s.startswith("principal:") for s in allowed[3:])
    assert "allowedNamespaces" not in request
