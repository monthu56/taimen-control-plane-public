"""Context profile grammar and deterministic anchor extraction (CP-ADR-0064).

The patterns are the software-delivery pack as memory-service's registry
returns it (``tests/fixtures/memory_graph_contract.json``): core runs the
domain's data, it does not know the domain.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import regex

from control_plane.domain.context_schema import (
    MAX_ANCHORS,
    MAX_MATCHES,
    anchor_candidates,
    extract_identifiers,
    parse_context_schema,
)
from control_plane.domain.errors import ValidationError

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_graph_contract.json").read_text()
)
PATTERNS = {
    spec["kind"]: tuple(regex.compile(p) for p in spec["idPatterns"])
    for spec in CONTRACT["packages"]["software-delivery@1"]["kinds"]
    if spec.get("idPatterns")
}

# The profiles of TAI-ADR-0042 p.3, in the pack's actual vocabulary.
CODING = {
    "anchors": [
        {"from": "description", "kinds": ["endpoint", "adr", "event", "table"]},
        {
            "from": "$.spawnedBy.artifact[commit].diffPaths",
            "kind": "source_file",
            "via": "defined_in",
        },
    ],
    "traverse": [
        {"relation": "calls", "direction": "in", "depth": 1, "limit": 20},
        {"relation": "defined_in", "depth": 1},
        {"relation": "governs", "direction": "in", "from": "previous"},
    ],
    "asOf": "taskCreated",
    "budgetTokens": 4000,
}
PROCUREMENT = {
    "anchors": [
        {"from": "$.customFields.okpdCodes", "kind": "okpd_code"},
        {"from": "$.customFields.customerInn", "kind": "counterparty"},
    ],
    "traverse": [{"relation": "requires", "depth": 2}, {"relation": "staffed_by"}],
    "asOf": "origin",
}


def _path(document: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        parse_context_schema(document)
    assert caught.value.code == "invalid_context_schema"
    return str(caught.value.details["path"])


def test_empty_document_is_no_profile() -> None:
    assert parse_context_schema({}) is None
    assert parse_context_schema(None) is None


def test_profiles_of_the_adr_parse() -> None:
    coding = parse_context_schema(CODING)
    assert coding is not None
    text, artifact = coding.anchors
    assert text.textual and text.kinds == ("endpoint", "adr", "event", "table")
    # The short artifact form reads the artifact's metadata.
    assert (artifact.path.root, artifact.path.artifact_type, artifact.path.metadata_key) == (
        "spawnedBy",
        "commit",
        "diffPaths",
    )
    assert not artifact.textual and artifact.via == "defined_in"
    assert [s.to_request() for s in coding.traverse] == [
        {"relation": "calls", "direction": "in", "depth": 1, "limit": 20, "from": "anchors"},
        {"relation": "defined_in", "direction": "out", "depth": 1, "limit": 20, "from": "anchors"},
        {"relation": "governs", "direction": "in", "depth": 1, "limit": 20, "from": "previous"},
    ]
    assert coding.budget_tokens == 4000
    procurement = parse_context_schema(PROCUREMENT)
    assert procurement is not None and procurement.as_of == "origin"
    assert procurement.anchors[0].path.key == "okpdCodes"
    assert procurement.roots == frozenset({"task"})


@pytest.mark.parametrize(
    ("document", "path"),
    [
        ([], "contextSchema"),
        ({"traverse": []}, "contextSchema.anchors"),
        ({"anchors": [{"from": "description"}] * 21}, "contextSchema.anchors"),
        ({"anchors": [{"kind": "adr"}]}, "contextSchema.anchors[0].from"),
        ({"anchors": [{"from": "$.approval.comment"}]}, "contextSchema.anchors[0].from"),
        ({"anchors": [{"from": "$.description!"}]}, "contextSchema.anchors[0].from"),
        ({"anchors": [{"from": "$.nope"}]}, "contextSchema.anchors[0].from"),
        (
            {"anchors": [{"from": "description", "kind": "a", "kinds": []}]},
            "contextSchema.anchors[0]",
        ),
        ({"anchors": [{"from": "description", "via": "Calls"}]}, "contextSchema.anchors[0].via"),
        ({"anchors": [{"from": "description", "why": 1}]}, "contextSchema.anchors[0]"),
        (
            {
                "anchors": [{"from": "description"}],
                "traverse": [{"relation": "calls", "direction": "up"}],
            },
            "contextSchema.traverse[0].direction",
        ),
        (
            {
                "anchors": [{"from": "description"}],
                "traverse": [{"relation": "calls", "from": "x"}],
            },
            "contextSchema.traverse[0].from",
        ),
        (
            {
                "anchors": [{"from": "description"}],
                "traverse": [{"relation": "calls", "depth": True}],
            },
            "contextSchema.traverse[0].depth",
        ),
        ({"anchors": [{"from": "description"}], "traverse": [{}] * 11}, "contextSchema.traverse"),
        ({"anchors": [{"from": "description"}], "budgetTokens": 0}, "contextSchema.budgetTokens"),
    ],
)
def test_invalid_profiles(document: Any, path: str) -> None:
    assert _path(document) == path


def test_extraction_is_deterministic_and_keeps_the_precise_form() -> None:
    text = (
        "Консоль шлёт limit в GET /api/v1/runs/{run_id}/checkpoints. См. CP-ADR-0058 и "
        "ADR-0042. Проверь POST /tasks/{task_id}:claim, событие task.claimed и файл "
        "src/control_plane/api/v1/claims.py."
    )
    found = extract_identifiers(text, PATTERNS, ["endpoint", "adr", "event"])
    assert found == [
        # Kinds in the declared order; a path inside "POST <path>" and
        # ADR-0058 inside CP-ADR-0058 are dropped; sentence dots are not ids.
        ("endpoint", "GET /api/v1/runs/{run_id}/checkpoints"),
        ("endpoint", "POST /tasks/{task_id}:claim"),
        ("adr", "CP-ADR-0058"),
        ("adr", "ADR-0042"),
        ("event", "task.claimed"),
    ]
    # Without declared kinds every kind of the catalog extracts; a file name
    # inside a path is not an event.
    every = extract_identifiers(text, PATTERNS)
    assert ("source_file", "src/control_plane/api/v1/claims.py") in every
    assert ("event", "claims.py") not in every
    assert extract_identifiers(text, PATTERNS) == every  # same input, same answer


def test_candidates_are_sent_as_written_deduplicated_and_capped() -> None:
    schema = parse_context_schema(
        {
            "anchors": [
                {"from": "description", "kinds": ["endpoint"]},
                {"from": "$.customFields.codes", "kind": "okpd_code"},
                {"from": "$.customFields.files", "kinds": ["source_file", "table"]},
            ]
        }
    )
    assert schema is not None
    candidates = anchor_candidates(
        schema,
        {
            "description": "POST /goals/{goal_id} and POST /goals/{goal_id} again",
            "$.customFields.codes": ["26.20.1", " ", 7],
            "$.customFields.files": "control-plane:src/app.py",
        },
        PATTERNS,
    )
    assert [c.to_request() for c in candidates] == [
        {"kind": "endpoint", "value": "POST /goals/{goal_id}"},
        {"kind": "okpd_code", "value": "26.20.1"},
        # Several kinds: the value goes without a hint, Memory resolves the key.
        {"value": "control-plane:src/app.py"},
    ]
    many = anchor_candidates(
        parse_context_schema({"anchors": [{"from": "$.customFields.codes", "kind": "adr"}]}),  # type: ignore[arg-type]
        {"$.customFields.codes": [f"CP-{i:04d}" for i in range(50)]},
        PATTERNS,
    )
    assert len(many) == MAX_ANCHORS


def test_nothing_to_extract_is_no_candidates() -> None:
    schema = parse_context_schema({"anchors": [{"from": "title", "kinds": ["endpoint"]}]})
    assert schema is not None
    assert anchor_candidates(schema, {"title": "Refactor"}, PATTERNS) == []
    assert anchor_candidates(schema, {"title": None}, PATTERNS) == []
    # No catalog (Memory unreachable): text extracts nothing, fields still anchor.
    assert extract_identifiers("POST /tasks", {}, ["endpoint"]) == []


def test_a_backtracking_pattern_times_out_instead_of_hanging() -> None:
    """Pack patterns are tenant data: a catastrophic one gives up, and text
    anchors of the profile are skipped with a warning; field values still anchor."""
    evil = {"endpoint": (regex.compile(r"(\w|\w\w)+\d$"),)}
    text = "a" * 5_000 + "!"
    with pytest.raises(TimeoutError):
        extract_identifiers(text, evil, timeout=0.05)
    schema = parse_context_schema(
        {
            "anchors": [
                {"from": "description", "kinds": ["endpoint"]},
                {"from": "$.customFields.endpoint", "kind": "endpoint"},
            ]
        }
    )
    assert schema is not None
    warnings: list[str] = []
    found = anchor_candidates(
        schema,
        {"description": text, "$.customFields.endpoint": "GET /runs"},
        evil,
        warnings=warnings,
    )
    assert [c.value for c in found] == ["GET /runs"]
    assert warnings == ["identifier extraction from description timed out"]


def test_matches_are_capped() -> None:
    many = {"adr": (regex.compile(r"ADR-\d+"),)}
    text = " ".join(f"ADR-{i}" for i in range(MAX_MATCHES + 500))
    found = extract_identifiers(text, many)
    assert len(found) == MAX_MATCHES


def test_containment_keeps_equal_spans_of_different_kinds() -> None:
    patterns = {
        "adr": (regex.compile(r"ADR-\d+"),),
        "doc": (regex.compile(r"ADR-\d+"),),
        "ref": (regex.compile(r"CP-ADR-\d+"),),
    }
    assert extract_identifiers("see ADR-1 and CP-ADR-2", patterns, ["adr", "doc"]) == [
        ("adr", "ADR-1"),
        ("doc", "ADR-1"),
    ]


def test_credential_shaped_values_are_not_anchors() -> None:
    schema = parse_context_schema({"anchors": [{"from": "$.customFields.ref", "kind": "adr"}]})
    assert schema is not None
    warnings: list[str] = []
    token = "ghp_" + "A" * 36
    found = anchor_candidates(
        schema, {"$.customFields.ref": [token, "ADR-0019"]}, PATTERNS, warnings=warnings
    )
    assert [c.value for c in found] == ["ADR-0019"]
    assert warnings == ["a credential-shaped value of $.customFields.ref is not an anchor"]
