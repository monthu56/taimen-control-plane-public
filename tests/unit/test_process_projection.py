"""Projection of cases and process versions into memory (CP-ADR-0076 §2-§3; P011).

The acceptance of P011 on a fixture process: the events a case and a version
leave in the journal become the nodes and edges of their projection; a
re-delivery leaves the graph as it was; a change of the data closes the
fact it replaces with a validity. The graph is ``ProjectionMemory`` — the
projection rules of memory-service.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from control_plane.application.context.mapping import (
    CaseDocument,
    map_event,
    map_event_parts,
    projection_stream,
)
from control_plane.infrastructure.db.models import Event
from control_plane.worker.context_adapter import DOCUMENT_CHUNK_CHARS, document_chunks
from tests.fake_projection_memory import ProjectionMemory, normalize_ts
from tests.unit.test_process_definition import _check

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
INSTANCE = uuid.uuid4()
WORKSPACE = uuid.uuid4()
CASE = "case:sample:S-1"
PROCESS = "process:sample-case"

_sequence = iter(range(1, 10_000))


def _event(
    event_type: str,
    payload: dict[str, Any],
    *,
    at: datetime,
    entity_type: str = "process_instance",
    entity_id: uuid.UUID = INSTANCE,
) -> Event:
    sequence = next(_sequence)
    return Event(
        sequence=sequence,
        tx_id=1000 + sequence,
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=None,
        session_id=None,
        correlation_id="c",
        causation_id=None,
        request_id="r",
        payload=payload,
        occurred_at=at,
    )


def _case_event(
    event_type: str,
    at: datetime,
    *,
    facts: dict[str, Any],
    entities: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> Event:
    return _event(
        event_type,
        {
            "instanceId": str(INSTANCE),
            "definitionKey": "sample-case",
            "version": 1,
            "instanceKey": "S-1",
            "workspaceId": str(WORKSPACE),
            "memory": {
                "case": {"kind": "case", "key": "sample:S-1", "title": "Sample S-1"},
                "facts": facts,
                "entities": entities if entities is not None else [],
                "documents": {"artifacts": ["sample-document"]},
            },
            **extra,
        },
        at=at,
    )


ELEMENTS = [
    {"id": "intake", "kind": "stage", "parent": None, "displayName": "Intake", "governedBy": []},
    {
        "id": "review",
        "kind": "step",
        "parent": "intake",
        "displayName": "Review",
        "governedBy": [{"document": "regulation:handbook", "section": "4.2"}],
    },
    # A step inside a step (its onTimeout) belongs to the stage around them.
    {"id": "fallback", "kind": "step", "parent": "review", "displayName": None, "governedBy": []},
    {"id": "ready", "kind": "milestone", "parent": "intake", "displayName": None, "governedBy": []},
    {
        "id": "level",
        "kind": "decision",
        "parent": None,
        "displayName": "Level",
        "governedBy": [{"document": "regulation:handbook"}],
    },
]


def _published(version: int, elements: list[dict[str, Any]], at: datetime) -> Event:
    return _event(
        "process.definition_published",
        {
            "key": "sample-case",
            "version": version,
            "definitionHash": "sha256:" + "0" * 64,
            "previousVersion": version - 1 or None,
            "workspaceId": str(WORKSPACE),
            "identityAgent": "sample-process",
            "displayName": "Sample case",
            "governedBy": [{"document": "regulation:charter"}],
            "elements": elements,
        },
        at=at,
        entity_type="process_definition",
        entity_id=uuid.uuid4(),
    )


def _deliver(memory: ProjectionMemory, events: list[Event]) -> None:
    """What the adapter does: each event with the earlier events of its stream."""
    for index, event in enumerate(events):
        stream = projection_stream(event)
        earlier = [e for e in events[:index] if stream and projection_stream(e) == stream]
        for observation in map_event_parts(event, earlier=earlier):
            memory.apply(observation)


def _facts(assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a["fact"] for a in assertions if a["assert"] == "fact"]


# --- the version of a process ----------------------------------------------------------


def test_a_published_version_projects_process_stages_and_steps() -> None:
    memory = ProjectionMemory()
    _deliver(memory, [_published(1, ELEMENTS, T0)])

    kinds = {key: node["type"] for key, node in memory.nodes.items()}
    assert kinds == {
        PROCESS: "process",
        f"{PROCESS}/intake": "process_stage",
        f"{PROCESS}/review": "process_step",
        f"{PROCESS}/fallback": "process_step",
        f"{PROCESS}/ready": "process_step",
        f"{PROCESS}/level": "process_step",
        "regulation:handbook": "regulation",  # a placeholder: the knowledge base owns it
        "regulation:charter": "regulation",
    }
    assert memory.nodes[PROCESS]["title"] == "Sample case"
    assert memory.nodes[PROCESS]["properties"]["version"] == 1
    assert [(s, o) for s, _, o, _ in memory.edges(predicate="stage_of")] == [
        (f"{PROCESS}/intake", PROCESS)
    ]
    assert [(s, o) for s, _, o, _ in memory.edges(predicate="step_of")] == [
        (f"{PROCESS}/fallback", f"{PROCESS}/intake"),
        (f"{PROCESS}/level", PROCESS),
        (f"{PROCESS}/ready", f"{PROCESS}/intake"),
        (f"{PROCESS}/review", f"{PROCESS}/intake"),
    ]
    assert [(s, o) for s, _, o, _ in memory.edges(predicate="regulates")] == [
        ("regulation:charter", PROCESS),
        ("regulation:handbook", f"{PROCESS}/level"),
        ("regulation:handbook", f"{PROCESS}/review"),
    ]
    # An edge of an observation has no attributes: the section is on the element.
    review = memory.nodes[f"{PROCESS}/review"]["properties"]
    assert review["regulatedBy"] == [{"document": "regulation:handbook", "section": "4.2"}]
    assert review["elementKind"] == "step"
    # Renamed relations (owner's decision 2026-09-27): no part_of, no governs.
    assert not {p for _, p, _, _ in memory.edges()} & {"part_of", "governs"}


def test_a_new_version_closes_what_it_no_longer_has() -> None:
    memory = ProjectionMemory()
    moved = [
        {**e, "parent": None} if e["id"] == "level" else e
        for e in ELEMENTS
        if e["id"] != "fallback"
    ]
    moved[1] = {**moved[1], "governedBy": []}  # review is no longer regulated
    later = T0 + timedelta(days=3)
    _deliver(memory, [_published(1, ELEMENTS, T0), _published(2, moved, later)])

    closed = {k[:3]: v for k, v in memory.facts.items() if v["valid_to"] is not None}
    assert closed == {
        (f"{PROCESS}/fallback", "step_of", f"{PROCESS}/intake"): {
            "valid_to": normalize_ts(later.isoformat())
        },
        ("regulation:handbook", "regulates", f"{PROCESS}/review"): {
            "valid_to": normalize_ts(later.isoformat())
        },
    }
    # What stays keeps its first start: one edge, not one per version.
    stays = memory.edges(f"{PROCESS}/intake", "stage_of")
    assert stays == [(f"{PROCESS}/intake", "stage_of", PROCESS, normalize_ts(T0.isoformat()))]
    assert memory.nodes[PROCESS]["properties"]["version"] == 2


# --- the case -------------------------------------------------------------------------


def test_a_case_projects_its_node_facts_entities_and_process() -> None:
    started = _case_event(
        "process.started",
        T0,
        facts={"amount": 100, "decision": None},
        entities=[
            {"kind": "party", "key": "7700000001", "name": "Party One", "rel": "counterpart"},
            {"kind": "region", "key": "77", "name": None, "rel": None},
        ],
    )
    observation = map_event(started)
    assert observation is not None
    assert observation["kind"] == "process.started"
    assert observation["content"] == "process.started: Sample S-1"
    assert {"type": "workspace", "id": str(WORKSPACE)} in observation["scopes"]
    assert observation["data"]["case"] == {"kind": "case", "key": CASE}
    # Only the projection reaches memory: no instance data, no instance key.
    assert "instanceKey" not in observation["data"]
    assert "memory" not in observation["data"]

    memory = ProjectionMemory()
    memory.apply(observation)
    assert memory.nodes[CASE] == {
        "type": "case",
        "title": "Sample S-1",
        "properties": {"amount": 100},  # a null fact is no fact
    }
    edges = {(p, o) for _, p, o, _ in memory.edges(CASE)}
    [fact] = [o for p, o in edges if p == "has_fact"]
    assert memory.nodes[fact] == {
        "type": "case_fact",
        "title": "amount: 100",
        "properties": {"name": "amount", "value": 100},
    }
    assert edges == {
        ("instance_of", PROCESS),
        ("has_fact", fact),
        ("counterpart", "party:7700000001"),
        ("involves", "region:77"),
    }
    assert memory.nodes["party:7700000001"]["title"] == "Party One"
    assert all(since == normalize_ts(T0.isoformat()) for *_, since in memory.edges(CASE))


def test_a_change_of_the_data_closes_the_fact_it_replaces() -> None:
    changed_at = T0 + timedelta(hours=5)
    events = [
        _case_event("process.started", T0, facts={"amount": 100, "decision": None}),
        _case_event(
            "process.data_changed",
            changed_at,
            facts={"amount": 120, "decision": "go"},
            changedFields=["amount", "decision"],
        ),
    ]
    memory = ProjectionMemory()
    _deliver(memory, events)

    history = sorted(
        (memory.nodes[o]["properties"]["value"], since, value["valid_to"])
        for (s, p, o, since), value in memory.facts.items()
        if s == CASE and p == "has_fact" and memory.nodes[o]["properties"]["name"] == "amount"
    )
    start, change = normalize_ts(T0.isoformat()), normalize_ts(changed_at.isoformat())
    assert history == [(100, start, change), (120, change, None)]
    assert memory.nodes[CASE]["properties"] == {"amount": 120, "decision": "go"}
    # What did not change keeps its edge from the start.
    assert (CASE, "instance_of", PROCESS, start) in memory.edges(CASE)

    closing = map_event(events[1], earlier=events[:1])
    assert closing is not None and closing["data"]["closedEdges"] == 1


def test_a_re_delivery_leaves_the_graph_as_it_was() -> None:
    events = [
        _published(1, ELEMENTS, T0 - timedelta(days=1)),
        _case_event("process.started", T0, facts={"amount": 100}),
        _case_event("process.data_changed", T0 + timedelta(hours=1), facts={"amount": 120}),
        _case_event(
            "process.completed", T0 + timedelta(hours=2), facts={"amount": 120}, outcome="done"
        ),
    ]
    memory = ProjectionMemory()
    _deliver(memory, events)
    before = memory.graph()

    # The same events again: the source identity stops them at the door…
    _deliver(memory, events)
    assert memory.graph() == before
    # …and the translation itself is a function of the journal: a memory that
    # lost its dedup (a rebuild) gets the same graph from the same events.
    rebuilt = ProjectionMemory()
    _deliver(rebuilt, events)
    _deliver(rebuilt, events[1:2])  # an old event delivered late changes nothing either
    assert rebuilt.graph()["facts"] == before["facts"]
    assert map_event(events[2], earlier=events[1:2]) == map_event(events[2], earlier=events[1:2])


def test_a_process_without_a_memory_section_says_nothing() -> None:
    event = _case_event("process.started", T0, facts={})
    event.payload = {**event.payload, "memory": None}
    assert map_event(event) is None
    assert map_event(_case_event("process.stage_entered", T0, facts={})) is None


def test_a_document_of_the_case_hangs_on_the_case() -> None:
    artifact = _event(
        "artifact.created",
        {"taskId": str(uuid.uuid4()), "type": "sample-document", "name": "Notice"},
        at=T0,
        entity_type="artifact",
    )
    plain = map_event(artifact)
    assert plain is not None and "assertions" not in plain
    document = CaseDocument(
        key=f"document:artifact/{artifact.entity_id}", case=CASE, workspace_id=str(WORKSPACE)
    )
    observation = map_event(artifact, document=document)
    assert observation is not None
    assert _facts(observation["assertions"]) == [
        {
            "subject": CASE,
            "predicate": "has_document",
            "object": document.key,
            "valid_from": T0.isoformat(),
        }
    ]
    assert {"type": "workspace", "id": str(WORKSPACE)} in observation["scopes"]


def test_a_large_projection_is_split_into_idempotent_parts() -> None:
    elements = [
        {"id": f"s{i}", "kind": "step", "parent": None, "displayName": None, "governedBy": []}
        for i in range(150)
    ]
    event = _published(1, elements, T0)
    parts = map_event_parts(event)
    assert [len(p["assertions"]) for p in parts] == [
        200,
        102,
    ]  # 151 nodes, 150 step_of, 1 regulates
    assert parts[0]["source"]["external_id"] == f"event:{event.id}"
    assert parts[1]["source"]["external_id"] == f"event:{event.id}/2"
    memory = ProjectionMemory()
    _deliver(memory, [event])
    assert len(memory.edges(predicate="step_of")) == 150


def test_unusable_keys_are_left_out_not_sent() -> None:
    event = _case_event(
        "process.started",
        T0,
        facts={},
        entities=[{"kind": "party", "key": "with space", "name": "X", "rel": "counterpart"}],
    )
    observation = map_event(event)
    assert observation is not None
    assert [a["entity"]["key"] for a in observation["assertions"] if a["assert"] == "entity"] == [
        CASE
    ]


# --- what the projection is built from --------------------------------------------------


def test_an_element_under_a_timer_or_branch_is_published_under_their_holder() -> None:
    spec: dict[str, Any] = {
        "version": 1,
        "displayName": "Sample",
        "identity": {"agent": "sample-process"},
        "owner": [{"role": "lead"}],
        "data": {"type": "object", "properties": {}},
        "start": {"on": {"observation": "sample.opened"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "timers": [{"id": "tick", "at": "P1D", "do": [{"id": "late", "wait": "PT1H"}]}],
                "steps": [
                    {
                        "id": "both",
                        "fork": {
                            "branches": [
                                {"id": "left", "do": [{"id": "inner", "wait": "PT1H"}]},
                                {"id": "right", "do": [{"id": "other", "wait": "PT2H"}]},
                            ]
                        },
                    }
                ],
            }
        ],
    }
    checked = _check(spec)
    assert not checked.errors, [p.out() for p in checked.errors]
    parents = {e.id: e.parent for e in checked.elements}
    assert parents == {
        "work": None,
        "late": "work",
        "both": "work",
        "inner": "both",
        "other": "both",
    }


def test_document_text_is_cut_into_paragraph_chunks() -> None:
    long = "x" * (DOCUMENT_CHUNK_CHARS + 10)
    chunks = document_chunks(f"First.\n\nSecond.\n\n\n{long}")
    assert [c["text"] for c in chunks] == [
        "First.\n\nSecond.",
        "x" * DOCUMENT_CHUNK_CHARS,
        "x" * 10,
    ]
    assert [c["order"] for c in chunks] == [0, 1, 2]
    assert document_chunks("") == []
