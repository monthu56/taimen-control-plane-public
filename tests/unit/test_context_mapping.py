"""Event → Observation mapping: whitelist, scopes, identity, minimization."""

import uuid
from datetime import UTC, datetime

from control_plane.application.context.mapping import (
    MAPPING_VERSION,
    is_memory_worthy,
    map_event,
)
from control_plane.infrastructure.db.models import Event


def _event(event_type: str, payload: dict | None = None, **kwargs) -> Event:
    return Event(
        sequence=kwargs.get("sequence", 7),
        tx_id=kwargs.get("tx_id", 100),
        id=kwargs.get("id", uuid.uuid4()),
        tenant_id=kwargs.get("tenant_id", uuid.uuid4()),
        event_type=event_type,
        entity_type=kwargs.get("entity_type", "task"),
        entity_id=kwargs.get("entity_id", uuid.uuid4()),
        actor_id=kwargs.get("actor_id", uuid.uuid4()),
        session_id=None,
        correlation_id="c",
        causation_id=None,
        request_id="r",
        payload=payload or {},
        occurred_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )


def test_noise_events_are_skipped() -> None:
    for noise in (
        "session.opened",
        "session.closed",
        "session.expired",
        "claim.released",
        "claim.expired",
        "run.checkpointed",
        "run.cancel_requested",
        "api_key.created",
        "api_key.revoked",
    ):
        assert not is_memory_worthy(noise)
        assert map_event(_event(noise)) is None


def test_task_completed_maps_with_stable_identity() -> None:
    event = _event("task.completed", {"publicId": "TASK-1", "title": "Fix race"})
    observation = map_event(event)
    assert observation is not None
    assert observation["source"] == {
        "system": "control-plane",
        "stream": "domain-events",
        "external_id": f"event:{event.id}",
    }
    assert observation["kind"] == "task.completed"
    assert observation["subject"] == {"type": "task", "id": str(event.entity_id)}
    assert observation["provenance"]["uri"] == f"control-plane://events/{event.id}"
    assert observation["data"]["mappingVersion"] == MAPPING_VERSION
    assert observation["data"]["eventSequence"] == event.sequence
    assert "Fix race" in observation["content"]
    assert observation["occurred_at"].startswith("2026-08-12T12:00")


def test_payload_fields_are_whitelisted_not_dumped() -> None:
    event = _event(
        "task.created",
        {
            "publicId": "TASK-2",
            "title": "T",
            "workspaceId": str(uuid.uuid4()),
            "apiKey": "cp_secret_leak",  # must never cross the boundary
            "internal": {"nested": True},
        },
    )
    observation = map_event(event)
    assert observation is not None
    assert "apiKey" not in observation["data"]
    assert "internal" not in observation["data"]
    assert "cp_secret_leak" not in str(observation)


def test_scopes_derived_from_entity_payload_and_actor() -> None:
    workspace_id = str(uuid.uuid4())
    event = _event("task.completed", {"workspaceId": workspace_id, "title": "x"})
    observation = map_event(event)
    assert observation is not None
    scopes = {(s["type"], s["id"]) for s in observation["scopes"]}
    assert ("task", str(event.entity_id)) in scopes
    assert ("workspace", workspace_id) in scopes
    assert ("principal", str(event.actor_id)) in scopes


def test_explicit_observation_forwards_kind_and_content() -> None:
    task_id = str(uuid.uuid4())
    event = _event(
        "observation.recorded",
        {
            "kind": "finding",
            "content": "The race is caused by xid/sequence inversion",
            "data": {"confidence": "high"},
            "taskId": task_id,
        },
        entity_type="observation",
    )
    observation = map_event(event)
    assert observation is not None
    assert observation["kind"] == "finding"
    assert observation["content"] == "The race is caused by xid/sequence inversion"
    assert observation["data"]["confidence"] == "high"
    scopes = {(s["type"], s["id"]) for s in observation["scopes"]}
    assert ("task", task_id) in scopes


def test_external_observation_forwards_origin_fields() -> None:
    """CP-ADR-0057: source/dedupKey/observedAt/supersedes/externalRef reach
    Memory in data; observedAt is when the fact occurred; the delivery
    identity stays the journal event, not the external source."""
    previous = str(uuid.uuid4())
    external_ref = {"system": "github", "id": "42", "url": "https://example.test/42"}
    event = _event(
        "observation.recorded",
        {
            "kind": "external_fact",
            "content": "Issue 42 closed",
            # Server-validated fields win over a same-named client data key.
            "data": {"source": "spoofed", "confidence": "high"},
            "source": "github",
            "dedupKey": "issue-42@closed",
            "observedAt": "2026-08-10T09:30:00+00:00",
            "supersedes": previous,
            "externalRef": external_ref,
        },
        entity_type="observation",
    )
    observation = map_event(event)
    assert observation is not None
    data = observation["data"]
    assert data["source"] == "github"
    assert data["dedupKey"] == "issue-42@closed"
    assert data["observedAt"] == "2026-08-10T09:30:00+00:00"
    assert data["supersedes"] == previous
    assert data["externalRef"] == external_ref
    assert data["confidence"] == "high"
    assert observation["occurred_at"] == "2026-08-10T09:30:00+00:00"
    assert observation["source"]["external_id"] == f"event:{event.id}"


def test_legacy_observation_without_origin_fields_maps_as_before() -> None:
    event = _event(
        "observation.recorded",
        {"kind": "note", "content": "old", "data": {"k": 1}},
        entity_type="observation",
    )
    observation = map_event(event)
    assert observation is not None
    assert observation["occurred_at"].startswith("2026-08-12T12:00")
    assert set(observation["data"]) == {"k", "mappingVersion", "eventSequence"}


def test_actor_attribution_from_journal_not_payload() -> None:
    event = _event("task.completed", {"title": "x", "actor": "spoofed"})
    observation = map_event(event)
    assert observation is not None
    assert observation["actor"] == {"type": "principal", "id": str(event.actor_id)}


def test_every_retained_type_produces_valid_observation() -> None:
    from control_plane.application.context.mapping import _RETAINED

    for event_type in _RETAINED:
        observation = map_event(_event(event_type, {"kind": "note", "content": "x"}))
        assert observation is not None, event_type
        assert observation["content"], event_type
        assert observation["source"]["external_id"].startswith("event:"), event_type


def test_structured_assertions_keep_server_authority():
    assertions = [{"assert": "entity", "entity": {"key": "company:x", "type": "company"}}]
    event = _event(
        "observation.recorded",
        {
            "kind": "external_fact",
            "content": "Public company",
            "assertions": assertions,
            "data": {"namespace": "foreign", "actor": "spoof", "assertions": ["untrusted"]},
        },
    )
    observation = map_event(event)
    assert observation["assertions"] == assertions
    assert observation["actor"]["id"] == str(event.actor_id)
    assert observation["provenance"]["uri"] == f"control-plane://events/{event.id}"
    assert "namespace" not in observation
    assert observation["source"]["external_id"] == f"event:{event.id}"
