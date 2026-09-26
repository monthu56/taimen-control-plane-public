"""HTTP contract between the Control Plane and the real Memory Service."""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from control_plane.application.context.mapping import map_event
from control_plane.infrastructure.context_provider.base import ContextProviderError
from control_plane.infrastructure.context_provider.http import HttpContextProvider
from control_plane.infrastructure.db.models import Event
from tests.contract.conftest import MEMORY_KEY, MEMORY_URL, pytestmark  # noqa: F401


def _cp_event(event_type: str = "task.completed", **payload: Any) -> Event:
    return Event(
        sequence=1,
        tx_id=1,
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        event_type=event_type,
        entity_type="task",
        entity_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        session_id=None,
        correlation_id="c",
        causation_id=None,
        request_id="r",
        payload={"title": "Fix the race", "publicId": "TASK-1", **payload},
        occurred_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


async def test_translated_event_is_accepted(provider: HttpContextProvider, namespace: str) -> None:
    observation = map_event(_cp_event())
    assert observation is not None
    result = await provider.ingest_batch(namespace=namespace, observations=[observation])
    assert result.accepted == 1
    assert result.failed == 0
    assert result.fully_delivered


async def test_same_source_identity_deduplicates(
    provider: HttpContextProvider, namespace: str
) -> None:
    observation = map_event(_cp_event())
    assert observation is not None
    first = await provider.ingest_batch(namespace=namespace, observations=[observation])
    second = await provider.ingest_batch(namespace=namespace, observations=[observation])
    assert first.accepted == 1 and first.duplicates == 0
    assert second.accepted == 0 and second.duplicates == 1
    assert second.fully_delivered  # duplicates are success, not failure


async def test_scopes_round_trip_through_retrieval(
    provider: HttpContextProvider, namespace: str
) -> None:
    event = _cp_event()
    task_id = str(event.entity_id)
    observation = map_event(event)
    assert observation is not None
    await provider.ingest_batch(namespace=namespace, observations=[observation])

    pack = await provider.build_context(
        namespace=namespace,
        request={
            "query": "fix the race",
            "scopes": [f"task:{task_id}"],
            "anchors": [f"task:{task_id}"],
            "max_tokens": 4000,
        },
    )
    assert pack["trace_id"].startswith("ctx-")
    assert pack["token_estimate"] <= 4000
    texts = [
        item.get("text", "") for section in pack["sections"] for item in section.get("items", [])
    ]
    assert any("Fix the race" in t for t in texts), texts


async def test_context_pack_shape_and_provenance(
    provider: HttpContextProvider, namespace: str
) -> None:
    event = _cp_event()
    observation = map_event(event)
    assert observation is not None
    await provider.ingest_batch(namespace=namespace, observations=[observation])

    pack = await provider.build_context(
        namespace=namespace, request={"query": "race", "max_tokens": 2000}
    )
    for key in ("sections", "sources", "token_estimate", "budget", "trace_id"):
        assert key in pack, key
    # Provenance of derived items points back at the Control Plane journal.
    source_paths = {s.get("source_path", "") for s in pack["sources"]}
    all_paths = source_paths | {
        item.get("source_path", "")
        for section in pack["sections"]
        for item in section.get("items", [])
    }
    assert any(p.startswith("control-plane://events/") for p in all_paths), all_paths


async def test_ephemeral_context_is_compiled_but_never_persisted(
    provider: HttpContextProvider, namespace: str
) -> None:
    marker = f"EPHEMERAL-{uuid.uuid4().hex[:8]}"
    pack = await provider.build_context(
        namespace=namespace,
        request={
            "query": "current state",
            "ephemeral_context": {"claimHolder": marker},
            "max_tokens": 4000,
        },
    )
    current_items = [
        item
        for section in pack["sections"]
        if section["kind"] == "current"
        for item in section["items"]
    ]
    assert any(marker in item.get("text", "") for item in current_items)

    # Not persisted as an observation…
    assert MEMORY_URL is not None
    async with httpx.AsyncClient(
        base_url=MEMORY_URL, headers={"Authorization": f"Bearer {MEMORY_KEY}"}
    ) as http:
        listing = (
            await http.get("/api/memory/observations", params={"namespace": namespace})
        ).json()
        assert all(marker not in str(o) for o in listing.get("observations", []))
        # …and the persisted trace stores only byte length, not content.
        trace = (
            await http.get(
                f"/api/memory/context/trace/{pack['trace_id']}",
                params={"namespace": namespace},
            )
        ).json()
        assert marker not in str(trace)
        assert trace["request"]["ephemeral_bytes"] > 0


async def test_explicit_finding_flows_and_recalls(
    provider: HttpContextProvider, namespace: str
) -> None:
    event = _cp_event(
        event_type="observation.recorded",
        kind="finding",
        content="The race is caused by xid/sequence inversion in the journal",
        taskId=str(uuid.uuid4()),
    )
    event.entity_type = "observation"
    event.payload = {
        "kind": "finding",
        "content": "The race is caused by xid/sequence inversion in the journal",
        "taskId": event.payload["taskId"] if "taskId" in event.payload else str(uuid.uuid4()),
    }
    observation = map_event(event)
    assert observation is not None
    assert observation["kind"] == "finding"
    result = await provider.ingest_batch(namespace=namespace, observations=[observation])
    assert result.accepted == 1

    pack = await provider.build_context(
        namespace=namespace, request={"query": "what causes the race?", "max_tokens": 4000}
    )
    texts = [
        item.get("text", "") for section in pack["sections"] for item in section.get("items", [])
    ]
    assert any("xid/sequence inversion" in t for t in texts), texts


async def test_namespaces_isolate_tenants(provider: HttpContextProvider) -> None:
    ns_a = f"tenant:{uuid.uuid4()}"
    ns_b = f"tenant:{uuid.uuid4()}"
    observation = map_event(_cp_event())
    assert observation is not None
    await provider.ingest_batch(namespace=ns_a, observations=[observation])

    pack_b = await provider.build_context(
        namespace=ns_b, request={"query": "fix the race", "max_tokens": 4000}
    )
    texts = [
        item.get("text", "") for section in pack_b["sections"] for item in section.get("items", [])
    ]
    assert not any("Fix the race" in t for t in texts), "tenant leak across namespaces"


async def test_unreachable_provider_raises_retryable() -> None:
    dead = HttpContextProvider(
        base_url="http://127.0.0.1:1",  # nothing listens here
        api_key="x",
        timeout_seconds=0.5,
        ingest_timeout_seconds=0.5,
    )
    try:
        with pytest.raises(ContextProviderError) as excinfo:
            await dead.ingest_batch(namespace="tenant:x", observations=[])
        assert excinfo.value.retryable
    finally:
        await dead.aclose()


async def test_invalid_kind_is_permanent_rejection(
    provider: HttpContextProvider, namespace: str
) -> None:
    """The Memory contract rejects malformed kinds; the provider surfaces it
    as failed-in-batch (poison), never as silent success."""
    observation = map_event(_cp_event())
    assert observation is not None
    observation["kind"] = "Not Valid Kind!"
    result = await provider.ingest_batch(namespace=namespace, observations=[observation])
    assert result.failed == 1
    assert not result.fully_delivered
    assert result.errors and "kind" in str(result.errors[0]).lower()


# --- v0.5 project observations ------------------------------------------------


def _project_event(event_type: str, **payload: Any) -> Event:
    return Event(
        sequence=2,
        tx_id=2,
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        event_type=event_type,
        entity_type="project",
        entity_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        session_id=None,
        correlation_id="c",
        causation_id=None,
        request_id="r",
        trace_run_id="run_contract",
        payload=payload,
        occurred_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


async def test_project_observation_is_accepted_and_recallable(
    provider: HttpContextProvider, namespace: str
) -> None:
    """The narrow project mapping (ADR-0032) satisfies the real contract."""
    event = _project_event(
        "project.created",
        workspaceId=str(uuid.uuid4()),
        parentProjectId=str(uuid.uuid4()),
        templateKey="delivery",
        templateVersion=1,
        statusKey="discovery",
        systemStatusCategory="planned",
    )
    observation = map_event(event)
    assert observation is not None
    assert observation["data"]["mappingVersion"] == 2

    result = await provider.ingest_batch(namespace=namespace, observations=[observation])
    assert result.accepted == 1 and result.fully_delivered

    pack = await provider.build_context(
        namespace=namespace,
        request={
            "query": "delivery project",
            "subject": {"type": "project", "id": str(event.entity_id)},
            "scopes": [f"project:{event.entity_id}"],
            "anchors": [f"project:{event.entity_id}"],
            "max_tokens": 2000,
        },
    )
    assert isinstance(pack, dict)
    assert "sections" in pack


async def test_project_status_change_carries_no_custom_fields(
    provider: HttpContextProvider, namespace: str
) -> None:
    """Tenant-authored JSON must never reach durable memory (ADR-0032)."""
    event = _project_event(
        "project.status_changed",
        fromStatusKey="discovery",
        statusKey="delivery",
        systemStatusCategory="active",
        comment="kickoff",
        # Deliberately present in the payload and deliberately NOT whitelisted.
        customFields={"secretish": "must not leave"},
        config={"settings": {"tone": "formal"}},
    )
    observation = map_event(event)
    assert observation is not None
    assert set(observation["data"]) == {
        "fromStatusKey",
        "statusKey",
        "systemStatusCategory",
        "comment",
        "mappingVersion",
        "eventSequence",
        # The originating trace travels with the fact (ADR-0039).
        "traceRunId",
    }
    assert observation["data"]["traceRunId"] == "run_contract"
    result = await provider.ingest_batch(namespace=namespace, observations=[observation])
    assert result.fully_delivered


async def test_ingest_forwards_the_trace_header(
    provider: HttpContextProvider, namespace: str
) -> None:
    """X-Run-Id reaches the Memory Service without changing the outcome."""
    observation = map_event(_cp_event())
    assert observation is not None
    result = await provider.ingest_batch(
        namespace=namespace, observations=[observation], trace_run_id="run_contract_trace"
    )
    assert result.fully_delivered


# --- knowledge snapshots (CP-ADR-0060) ---------------------------------------


def _knowledge_snapshot(snapshot_id: str, observed_at: str) -> dict[str, Any]:
    from control_plane.api.v1.schemas import KnowledgeSnapshotRequest

    return KnowledgeSnapshotRequest.model_validate(
        {
            "workspaceId": str(uuid.uuid4()),
            "pack": "default",
            "source": "git:contract",
            "scope": "repo:contract",
            "snapshotId": snapshot_id,
            "observedAt": observed_at,
        }
    ).snapshot_document()


async def test_reconcile_accepts_the_core_body_and_rejects_stale(
    provider: HttpContextProvider, namespace: str
) -> None:
    newer = _knowledge_snapshot("snap-2", "2026-09-23T11:00:00Z")
    first = await provider.reconcile_snapshot(
        namespace=namespace, scopes=["workspace:w"], snapshot=newer
    )
    assert first["duplicate"] is False
    again = await provider.reconcile_snapshot(
        namespace=namespace, scopes=["workspace:w"], snapshot=newer
    )
    assert again["duplicate"] is True
    with pytest.raises(ContextProviderError) as info:
        await provider.reconcile_snapshot(
            namespace=namespace,
            scopes=["workspace:w"],
            snapshot=_knowledge_snapshot("snap-1", "2026-09-23T10:00:00Z"),
        )
    assert info.value.status == 409


async def test_reconcile_rejects_an_invalid_snapshot_with_400(
    provider: HttpContextProvider, namespace: str
) -> None:
    broken = {**_knowledge_snapshot("snap-1", "2026-09-23T10:00:00Z"), "observedAt": "never"}
    with pytest.raises(ContextProviderError) as info:
        await provider.reconcile_snapshot(namespace=namespace, scopes=[], snapshot=broken)
    assert info.value.status == 400


async def test_namespace_kinds_reject_an_unknown_pinned_pack(
    provider: HttpContextProvider, namespace: str
) -> None:
    with pytest.raises(ContextProviderError) as info:
        await provider.set_namespace_kinds(
            namespace=namespace, packages=[f"missing-{uuid.uuid4().hex[:8]}@1"], strict=False
        )
    assert info.value.status == 404
