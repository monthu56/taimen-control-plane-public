"""Snapshot preview and apply by state (CP-ADR-0060 amendment 2026-09-28, K008).

``POST /knowledge/snapshots:preview`` is Memory's reconcile with ``dryRun``:
the plan and its ``stateToken`` come back as is, and nothing is journaled.
``expectedState`` of ``POST /knowledge/snapshots`` goes to Memory next to the
snapshot; a state that moved on is ``409 snapshot_stale``.
"""

from typing import Any

import httpx
from fastapi import FastAPI

from tests.helpers import (
    FakeKnowledge,
    auth,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
)
from tests.helpers import (
    knowledge_snapshot as snapshot,
)

PREVIEW = "/api/v1/knowledge/snapshots:preview"
APPLY = "/api/v1/knowledge/snapshots"


async def _setup(client: httpx.AsyncClient) -> tuple[str, str, dict[str, Any]]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, writer = await create_agent_with_key(
        client, admin_key, name="writer", permissions=["observations.write"]
    )
    root = await create_workspace(client, admin_key, "root")
    return admin_key, writer, root


async def _knowledge_events(client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
    items = (await client.get("/api/v1/events", params={"limit": 200}, headers=auth(key))).json()[
        "items"
    ]
    return [e for e in items if e["type"].startswith("knowledge.")]


async def test_preview_is_memorys_dry_run_and_writes_no_event(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key, writer, root = await _setup(client)
    sub = await create_workspace(client, admin_key, "sub", parent_id=root["id"])
    changes = {
        "opened": [{"kind": "regulation", "key": "regulation:new"}],
        "changed": [],
        "closed": [],
    }
    fake = FakeKnowledge(changes=changes)
    app.state.context_provider = fake
    try:
        response = await client.post(
            PREVIEW, json={**snapshot(), "workspaceId": sub["id"]}, headers=auth(writer)
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["dryRun"] is True
    assert plan["stateToken"] == "st1-0"
    assert plan["changes"] == changes
    [(kind, call)] = fake.calls
    assert kind == "reconcile"
    assert call["dry_run"] is True
    # Where and to whom, as for the snapshot itself: the root's namespace, the
    # sub-workspace's scope; the document carries no workspaceId.
    assert call["namespace"].endswith(f":ws:{root['id']}")
    assert call["scopes"] == [f"workspace:{sub['id']}"]
    assert "workspaceId" not in call["snapshot"]
    assert fake.applied == 0
    # A plan is not a fact: neither snapshot_reconciled nor knowledge.changed.
    assert await _knowledge_events(client, admin_key) == []


async def test_preview_takes_the_snapshot_permission_and_workspace(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key, _, root = await _setup(client)
    _, reader = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["tasks.read"]
    )
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        denied = await client.post(
            PREVIEW, json={**snapshot(), "workspaceId": root["id"]}, headers=auth(reader)
        )
        unknown = await client.post(
            PREVIEW,
            json={**snapshot(), "workspaceId": "00000000-0000-0000-0000-000000000001"},
            headers=auth(admin_key),
        )
        # The preview's body has no expectedState, nor namespace/scopes.
        extra = [
            await client.post(
                PREVIEW,
                json={**snapshot(), "workspaceId": root["id"], **field},
                headers=auth(admin_key),
            )
            for field in ({"expectedState": "st1-0"}, {"namespace": "tenant:x:ws:y"})
        ]
    finally:
        app.state.context_provider = None
    assert denied.status_code == 403
    assert unknown.status_code == 404
    assert [r.status_code for r in extra] == [400, 400]
    assert fake.calls == []
    disabled = await client.post(
        PREVIEW, json={**snapshot(), "workspaceId": root["id"]}, headers=auth(admin_key)
    )
    assert disabled.status_code == 503
    assert disabled.json()["error"]["code"] == "memory_disabled"


async def test_apply_by_the_previewed_state(client: httpx.AsyncClient, app: FastAPI) -> None:
    admin_key, writer, root = await _setup(client)
    body = {**snapshot(), "workspaceId": root["id"]}
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        plan = (await client.post(PREVIEW, json=body, headers=auth(writer))).json()
        applied = await client.post(
            APPLY, json={**body, "expectedState": plan["stateToken"]}, headers=auth(writer)
        )
    finally:
        app.state.context_provider = None
    assert applied.status_code == 200, applied.text
    assert applied.json()["dryRun"] is False
    [(_, preview_call), (_, apply_call)] = fake.calls
    assert preview_call["dry_run"] is True
    assert apply_call["expected_state"] == plan["stateToken"]
    assert apply_call.get("dry_run", False) is False
    # expectedState goes next to the snapshot, never into it.
    assert "expectedState" not in apply_call["snapshot"]
    assert apply_call["snapshot"] == preview_call["snapshot"]
    [event] = [
        e
        for e in await _knowledge_events(client, admin_key)
        if e["type"] == "knowledge.snapshot_reconciled"
    ]
    assert "expectedState" not in event["payload"]


async def test_a_state_that_moved_on_is_409_snapshot_stale(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key, writer, root = await _setup(client)
    body = {**snapshot(), "workspaceId": root["id"]}
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        plan = (await client.post(PREVIEW, json=body, headers=auth(writer))).json()
        # Another loader applies its snapshot of the same source in between.
        other = await client.post(
            APPLY, json={**body, "snapshotId": "snap-other"}, headers=auth(writer)
        )
        stale = await client.post(
            APPLY, json={**body, "expectedState": plan["stateToken"]}, headers=auth(writer)
        )
    finally:
        app.state.context_provider = None
    assert other.status_code == 200
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "snapshot_stale"
    assert error["details"] == {"memoryStatus": 409}
    assert fake.applied == 1
    # Only the other loader's snapshot is journaled.
    reconciled = [
        e
        for e in await _knowledge_events(client, admin_key)
        if e["type"] == "knowledge.snapshot_reconciled"
    ]
    assert [e["payload"]["snapshotId"] for e in reconciled] == ["snap-other"]


async def test_expected_state_is_bounded_like_memorys(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    _, writer, root = await _setup(client)
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        responses = [
            await client.post(
                APPLY,
                json={**snapshot(), "workspaceId": root["id"], "expectedState": token},
                headers=auth(writer),
            )
            for token in ("", "s" * 129)
        ]
    finally:
        app.state.context_provider = None
    assert [r.status_code for r in responses] == [400, 400]
    assert fake.calls == []


async def test_preview_failures_are_mapped_and_journal_nothing(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key, writer, root = await _setup(client)
    body = {**snapshot(), "workspaceId": root["id"]}
    answers: dict[str, httpx.Response] = {}
    try:
        for name, fake in {
            "invalid": FakeKnowledge(fail_status=400),
            "older": FakeKnowledge(fail_status=409),
            "down": FakeKnowledge(fail_status=503, retryable=True),
            # A Memory before the preview ignores dryRun and applies.
            "no_plan": FakeKnowledge(preview=False),
        }.items():
            app.state.context_provider = fake
            answers[name] = await client.post(PREVIEW, json=body, headers=auth(writer))
    finally:
        app.state.context_provider = None
    codes = {name: (r.status_code, r.json()["error"]["code"]) for name, r in answers.items()}
    assert codes == {
        "invalid": (422, "snapshot_invalid"),
        "older": (409, "snapshot_stale"),
        "down": (502, "memory_unavailable"),
        "no_plan": (502, "memory_unavailable"),
    }
    assert answers["down"].json()["error"]["details"]["retryable"] is True
    assert answers["no_plan"].json()["error"]["details"] == {
        "memoryStatus": 200,
        "retryable": False,
    }
    for response in answers.values():
        assert "memory said" not in response.text
    assert await _knowledge_events(client, admin_key) == []


async def test_preview_takes_the_snapshot_body_limit(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    _, writer, root = await _setup(client)
    settings = app.state.settings
    filler = "x" * 1000
    count = settings.max_body_bytes * 2 // 1100
    app.state.context_provider = FakeKnowledge()
    try:
        big = await client.post(
            PREVIEW,
            json={
                **snapshot(
                    entities=[{"kind": "doc", "key": str(i), "t": filler} for i in range(count)]
                ),
                "workspaceId": root["id"],
            },
            headers=auth(writer),
        )
        too_big = await client.post(
            PREVIEW,
            content=b"{" + b" " * settings.knowledge_snapshot_max_body_bytes + b"}",
            headers={**auth(writer), "Content-Type": "application/json"},
        )
    finally:
        app.state.context_provider = None
    assert big.status_code == 200, big.text[:500]
    assert too_big.status_code == 413
