"""Processes and memory without a database (CP-ADR-0076 §4-§6; process-packages P010).

- the answer of a ``recall`` step normalized from Memory's typed packs:
  explicit links first, the read by similarity marked ``inferred``;
- a step's context profile as its task carries it, and the semantic part of
  a task pack;
- what a ``remember`` step writes: an observation with assertions keyed as
  memory resolves the anchors of a recall;
- ``mocks.recall`` of a package test, checked against the form of an answer;
- replay of an instance from its journal alone: zero discrepancies, and a
  changed answer of memory is found.
"""

import uuid
from typing import Any

import pytest

from control_plane.application.commands.process_instances import (
    REMEMBER_KIND,
    case_kind,
    remembered,
)
from control_plane.application.context.assertions import validate_assertions
from control_plane.application.context.graph import (
    SEMANTIC_REQUEST,
    GraphScope,
    entity_key,
    recall_answer,
    semantic_request,
    with_inferred,
)
from control_plane.application.queries.recall import process_recall_call
from control_plane.domain.context_schema import STEP_SOURCE, step_profile
from control_plane.domain.process_replay import MockError, mock_recall, replay
from control_plane.infrastructure.db.models import ProcessInstance
from tests.unit.test_process_contract import PACKAGE_TEST
from tests.unit.test_process_engine import CALENDARS, RECALL, Run


def _entity(key: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "natural_key": key,
        "kind": kind,
        "namespace": "t:ws:w",
        "title": key,
        "attributes": {},
        "evidence": "asserted",
        "anchor": False,
        **extra,
    }


def _fact(fact_id: str, subject: str, obj: str) -> dict[str, Any]:
    return {"fact_id": fact_id, "relation": "applies_to", "subject": subject, "object": obj}


def _pack(*entities: dict[str, Any], facts: list[dict[str, Any]] | None = None, **extra: Any):
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        by_kind.setdefault(entity["kind"], []).append(entity)
    return {
        "anchors": [],
        "sections": [{"kind": k, "items": v} for k, v in sorted(by_kind.items())],
        "facts": facts or [],
        "used": {
            "entities": [
                {"namespace": "t:ws:w", "natural_key": e["natural_key"]} for e in entities
            ],
            "facts": [f["fact_id"] for f in facts or []],
            "snapshots": [],
        },
        **extra,
    }


# --- the answer of a recall ------------------------------------------------------------------


def test_recall_answer_puts_explicit_links_first_and_marks_similarity() -> None:
    explicit = _pack(
        _entity("case:C-1", "case", anchor=True, valid_from="2026-01-01T00:00:00Z"),
        _entity("lesson:old", "lesson", valid_from="2026-01-02T00:00:00Z"),
        _entity("lesson:new", "lesson", valid_from="2026-03-01T00:00:00Z"),
        facts=[
            {
                "fact_id": "f1",
                "relation": "applies_to",
                "subject": "lesson:new",
                "object": "case:C-1",
            },
            {
                "fact_id": "f2",
                "relation": "applies_to",
                "subject": "lesson:old",
                "object": "case:C-1",
            },
        ],
    )
    semantic = _pack(
        _entity("lesson:new", "lesson"),
        _entity("lesson:similar", "lesson", evidence="inferred"),
    )
    answer = recall_answer(explicit, semantic)
    assert [(n["key"], n["inferred"]) for n in answer["nodes"]] == [
        ("lesson:new", False),  # the freshest first
        ("lesson:old", False),
        ("case:C-1", False),
        ("lesson:similar", True),  # the read by similarity after every explicit link
    ]
    assert answer["nodes"][2]["anchor"] is True
    assert {(e["from"], e["relation"], e["to"]) for e in answer["edges"]} == {
        ("lesson:new", "applies_to", "case:C-1"),
        ("lesson:old", "applies_to", "case:C-1"),
    }
    assert answer["truncated"] is False


def test_recall_answer_keeps_kinds_and_limit_and_reports_a_cut() -> None:
    explicit = _pack(
        _entity("case:C-1", "case", anchor=True),
        _entity("lesson:a", "lesson"),
        _entity("lesson:b", "lesson"),
        facts=[
            {"fact_id": "f", "relation": "applies_to", "subject": "lesson:a", "object": "case:C-1"}
        ],
    )
    answer = recall_answer(explicit, kinds=["lesson"], limit=1)
    assert [n["key"] for n in answer["nodes"]] == ["lesson:a"]
    assert answer["truncated"] is True
    # An edge stays while one of its ends does.
    assert [e["to"] for e in answer["edges"]] == ["case:C-1"]

    cut = _pack(_entity("case:C-1", "case"), stats={"steps": [{"truncated": True}]})
    assert recall_answer(cut)["truncated"] is True


def test_with_inferred_appends_what_similarity_found_after_the_pack() -> None:
    pack = _pack(_entity("case:C-1", "case", anchor=True))
    found = _pack(_entity("case:C-1", "case"), _entity("lesson:x", "lesson", evidence="inferred"))
    merged = with_inferred(pack, found)
    assert [s["kind"] for s in merged["sections"]] == ["case", "lesson"]
    assert merged["sections"][-1]["inferred"] is True
    assert merged["sections"][-1]["items"][0]["evidence"] == "inferred"
    assert [e["natural_key"] for e in merged["used"]["entities"]] == ["case:C-1", "lesson:x"]
    assert with_inferred(pack, _pack()) is pack


def test_semantic_request_reads_the_text_by_similarity_only() -> None:
    assert semantic_request("closed cases like this", "2026-03-02T09:00:00Z") == {
        "anchors": [{"value": "closed cases like this"}],
        "traverse": [],
        "allow_semantic": True,
        "as_of": "2026-03-02T09:00:00Z",
    }
    assert SEMANTIC_REQUEST == "semantic"


def test_process_recall_call_sends_computed_keys_and_skips_empty_ones() -> None:
    scope = GraphScope(namespace="t", namespaces=["t", "t:ws:w"])
    call = process_recall_call(
        {
            "anchors": [
                {"case": True, "kind": "case", "key": "case:C-1"},
                {"kind": "person", "key": None},
                {"kind": "document", "key": "doc-1", "via": "governs"},
            ],
            "traverse": [{"relation": "customer", "direction": "in"}],
            "kinds": ["case"],
            "query": " similar ",
            "limit": 5,
            "asOf": "2026-03-02T09:00:00Z",
        },
        scope,
    )
    assert [(c.kind, c.value, c.via) for c in call.anchors] == [
        ("case", "case:C-1", None),
        ("document", "doc-1", "governs"),
    ]
    assert call.traverse == [
        {"relation": "customer", "direction": "in", "depth": 1, "limit": 20, "from": "anchors"}
    ]
    assert (call.query, call.kinds, call.limit, call.as_of) == (
        "similar",
        ["case"],
        5,
        "2026-03-02T09:00:00Z",
    )


# --- the context of a step's task ------------------------------------------------------------


def test_step_profile_takes_computed_anchors_as_values() -> None:
    schema = step_profile(
        {
            "anchors": [
                {"case": True, "kind": "case", "key": "case:C-1"},
                {"kind": "legal_entity", "key": "7700000000", "via": "customer"},
                {"kind": "person", "key": None},
            ],
            "traverse": [{"relation": "applies_to", "direction": "in", "depth": 1}],
            "semantic": False,
            "budgetTokens": 2000,
        }
    )
    assert schema is not None
    assert schema.anchors == ()
    assert [(c.kind, c.value, c.via, c.source) for c in schema.values] == [
        ("case", "case:C-1", None, STEP_SOURCE),
        ("legal_entity", "7700000000", "customer", STEP_SOURCE),
    ]
    assert [s.to_request()["relation"] for s in schema.traverse] == ["applies_to"]
    assert (schema.semantic, schema.budget_tokens) == (False, 2000)
    assert step_profile(None) is None
    assert step_profile({"anchors": [{"kind": "case", "key": "k"}]}).semantic is True  # type: ignore[union-attr]


# --- remember --------------------------------------------------------------------------------


def _instance() -> ProcessInstance:
    return ProcessInstance(id=uuid.uuid4(), definition_key="purchase")


def test_remember_facts_are_properties_of_the_case_node() -> None:
    instance = _instance()
    written = remembered(
        {
            "element": "price",
            "case": "purchase:0001",
            "facts": {"nmck": 12000000, "total": {"net": 200}},
        },
        case_kind({"memory": {"case": {"kind": "purchase"}}}),
        instance,
    )
    assert written["kind"] == REMEMBER_KIND
    assert written["assertions"] == [
        {
            "assert": "entity",
            "entity": {
                "key": "purchase:purchase:0001",
                "type": "purchase",
                "properties": {"nmck": 12000000, "total": {"net": 200}},
            },
        }
    ]
    assert validate_assertions(written["assertions"])
    assert written["data"] == {
        "processInstanceId": str(instance.id),
        "process": "purchase",
        "element": "price",
        "case": {"kind": "purchase", "key": "purchase:0001"},
    }
    assert "nmck = 12000000" in written["content"]


def test_remember_entity_links_are_facts_between_keys_memory_resolves() -> None:
    written = remembered(
        {
            "element": "retrospective",
            "case": "purchase:0001",
            "entity": {
                "kind": "lesson",
                "key": "lesson:i:1",
                "text": "the customer bargains",
                "links": [
                    {"rel": "learned_from", "kind": "case", "key": "purchase:0001"},
                    {"rel": "applies_to", "kind": "legal_entity", "key": "7700000000"},
                    {"rel": "applies_to", "kind": "legal_entity", "key": None},
                ],
            },
            "evidence": [3, 5],
        },
        case_kind({}),
        _instance(),
    )
    lesson = entity_key("lesson", "lesson:i:1")
    facts = [a["fact"] for a in written["assertions"] if a["assert"] == "fact"]
    assert facts == [
        {"subject": lesson, "predicate": "learned_from", "object": "case:purchase:0001"},
        {"subject": lesson, "predicate": "applies_to", "object": "legal_entity:7700000000"},
    ]
    assert written["assertions"][0]["entity"]["properties"] == {"text": "the customer bargains"}
    assert validate_assertions(written["assertions"])
    assert written["content"] == "the customer bargains"
    assert written["data"]["evidence"] == [3, 5]
    assert written["data"]["case"] == {"kind": "case", "key": "purchase:0001"}


# --- mocks.recall ----------------------------------------------------------------------------


INTENT = {
    "recallId": "r",
    "activityId": "a",
    "element": "recall-history",
    "anchors": [{"case": True, "kind": "case", "key": "purchase:1"}],
    "query": "history",
}


def test_mock_recall_answers_the_step_it_names() -> None:
    body = mock_recall(PACKAGE_TEST["mocks"]["recall"], INTENT)
    assert body == {
        "activityId": "a",
        "status": "completed",
        "result": {
            "nodes": [
                {"kind": "lesson", "key": "lesson:1", "text": "заказчик снижает цену на переторжке"}
            ],
            "edges": [],
            "truncated": False,
        },
    }
    with pytest.raises(MockError) as missing:
        mock_recall(PACKAGE_TEST["mocks"]["recall"], {**INTENT, "element": "other"})
    assert missing.value.code == "mock_missing"


def test_mock_recall_when_timeout_and_error() -> None:
    mocks = [
        {"step": "recall-history", "when": "input.query == 'nothing'", "timeout": True},
        {"when": "input.query == 'down'", "error": {"type": "memory_unavailable"}},
        {"output": {"nodes": [], "edges": []}},
    ]
    used: dict[int, int] = {}
    assert mock_recall(mocks, {**INTENT, "query": "nothing"}, used)["status"] == "timed_out"
    down = mock_recall(mocks, {**INTENT, "query": "down"}, used)
    assert (down["status"], down["reason"]) == ("timed_out", "memory_unavailable")
    assert mock_recall(mocks, INTENT, used)["result"] == {
        "nodes": [],
        "edges": [],
        "truncated": False,
    }
    assert used == {0: 1, 1: 1, 2: 1}


def test_mock_recall_output_must_be_a_memory_answer() -> None:
    with pytest.raises(MockError) as invalid:
        mock_recall([{"output": {"nodes": [{"kind": "lesson"}]}}], INTENT)
    assert invalid.value.code == "mock_output_invalid"
    assert invalid.value.details["path"] == "/nodes/0"
    with pytest.raises(MockError):
        mock_recall([{"output": {"rows": []}}], INTENT)


# --- replay ----------------------------------------------------------------------------------


def _journal(run: Run) -> list[dict[str, Any]]:
    return [
        {
            "seq": seq,
            "input": given.out(),
            "decisions": [d.out() for d in decisions],
            "intents": [{**i.out(), "executed": {"ok": True}} for i in intents],
            "calendars": {"ru": 1},
        }
        for seq, (given, decisions, intents) in enumerate(run.records)
    ]


def _recalled() -> Run:
    run = Run(RECALL)
    run.start()
    activity = run.activity("history")
    answer = {"nodes": [{"kind": "case", "key": "case:N-0"}], "edges": [], "truncated": False}
    run.feed("recall", {"activityId": activity["id"], "status": "completed", "result": answer})
    return run


def test_replay_takes_the_recall_answer_from_the_journal() -> None:
    run = _recalled()
    result = replay(run.definition, _journal(run), lambda named: CALENDARS)
    assert result.discrepancies == []
    assert result.steps == 2
    assert result.state == run.state


def test_replay_finds_a_journal_that_does_not_match() -> None:
    run = _recalled()
    journal = _journal(run)
    # Another answer of memory than the one recorded: the hash of the decision differs.
    journal[1]["input"]["body"]["result"]["nodes"] = [{"kind": "case", "key": "case:N-9"}]
    result = replay(run.definition, journal, lambda named: CALENDARS)
    assert {d.field for d in result.discrepancies} == {"decisions", "intents"}
    assert all(d.seq == 1 for d in result.discrepancies)
