"""Work graph documents: origin, acceptance checks, evidence (CP-ADR-0062).

The domain is the one place these documents are validated, so every writer —
HTTP, an approval outcome, a future rule engine — stores the same shape.
"""

import uuid
from typing import Any

import pytest

from control_plane.application.context.mapping import map_event
from control_plane.domain.artifact_schema import parse_artifact_schema
from control_plane.domain.errors import ValidationError
from control_plane.domain.work_graph import (
    CHECK_KINDS,
    INVALID_ACCEPTANCE_SPEC,
    MAX_CHECKS,
    SPEC_KEYS,
    check_evidence_against_acceptance,
    check_order_advice,
    check_spec,
    context_pack_targets,
    evidence_targets,
    normalize_checks,
    normalize_evidence,
    normalize_origin,
    origin_summary,
    output_checks,
)
from tests.unit.test_context_mapping import _event

OBS = str(uuid.uuid4())
ART = str(uuid.uuid4())


def _code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        fn(*args, **kwargs)
    return caught.value.code


def test_rule_origin_normalizes_and_keeps_its_evidence() -> None:
    origin = normalize_origin(
        {
            "kind": "rule",
            "ruleId": "decision-drift/v1",
            "ref": "  decision:0048 ",
            "evidence": [
                {"kind": "observation", "observationId": OBS.upper(), "note": " why "},
                {"kind": "artifact", "artifactId": ART},
            ],
        }
    )
    assert origin == {
        "kind": "rule",
        "ref": "decision:0048",
        "ruleId": "decision-drift/v1",
        "evidence": [
            {"kind": "observation", "observationId": OBS, "note": "why"},
            {"kind": "artifact", "artifactId": ART},
        ],
    }
    assert evidence_targets(origin["evidence"]) == ({uuid.UUID(OBS)}, {uuid.UUID(ART)})


@pytest.mark.parametrize(
    "origin",
    [
        {"kind": "rule", "evidence": [{"kind": "artifact", "artifactId": ART}]},
        {"kind": "rule", "ruleId": "drift"},
        {
            "kind": "rule",
            "ruleId": " spaced",
            "evidence": [{"kind": "artifact", "artifactId": ART}],
        },
        {"kind": "human", "ruleId": "drift"},
        {"kind": "process"},
        {"kind": "parent"},
        {"kind": "external"},
        {"kind": "oracle"},
        {"kind": "human", "extra": 1},
        {"kind": "human", "evidence": [{"kind": "artifact", "artifactId": ART, "check": "a"}]},
        "human",
    ],
)
def test_invalid_origins(origin: Any) -> None:
    assert _code(normalize_origin, origin) == "invalid_origin"


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([{"kind": "observation"}], "invalid_evidence"),
        ([{"kind": "observation", "observationId": "not-a-uuid"}], "invalid_evidence"),
        ([{"kind": "artifact", "observationId": OBS}], "invalid_evidence"),
        ([{"kind": "external", "externalRef": {"system": "vcs"}}], "invalid_evidence"),
        (
            [{"kind": "external", "externalRef": {"system": "a", "id": "b", "x": 1}}],
            "invalid_evidence",
        ),
        ([{"kind": "rumour"}], "invalid_evidence"),
        ([{"kind": "artifact", "artifactId": ART, "check": "Not A Key"}], "invalid_evidence"),
        ([{"kind": "artifact", "artifactId": ART}] * 2, "duplicate_evidence"),
        (
            [{"kind": "artifact", "artifactId": ART, "note": "token=AKIAABCDEFGHIJKLMNOP"}],
            "secret_material_rejected",
        ),
        ({"kind": "artifact"}, "invalid_evidence"),
    ],
)
def test_invalid_evidence(items: Any, code: str) -> None:
    assert _code(normalize_evidence, items) == code


def test_the_same_fact_may_speak_to_different_checks() -> None:
    items = [
        {"kind": "artifact", "artifactId": ART, "check": "a"},
        {"kind": "artifact", "artifactId": ART, "check": "b"},
    ]
    assert len(normalize_evidence(items)) == 2


def test_checks_have_unique_keys_known_kinds_and_safe_specs() -> None:
    check = {"key": "tests-pass", "kind": "deterministic", "description": "green"}
    assert normalize_checks([check]) == [check]
    assert _code(normalize_checks, [check, check]) == "duplicate_check_key"
    assert _code(normalize_checks, [{**check, "kind": "vibes"}]) == "invalid_acceptance"
    assert _code(normalize_checks, [{**check, "description": " "}]) == "invalid_acceptance"
    assert _code(normalize_checks, [{**check, "spec": {"password": "x"}}]) == (
        "secret_material_rejected"
    )
    assert _code(normalize_checks, [{**check, "spec": "run it"}]) == "invalid_document"
    many = [{**check, "key": f"k{i}"} for i in range(MAX_CHECKS + 1)]
    assert _code(normalize_checks, many) == "payload_too_large"


# --- acceptance spec grammar (CP-ADR-0067) -------------------------------------

PRINCIPAL = str(uuid.uuid4())

VALID_SPECS: list[tuple[str, dict[str, Any]]] = [
    ("deterministic", {"skill": "check.sample@1"}),
    (
        "deterministic",
        {
            "skill": "check.sample@1.2.0",
            "inputs": {
                "subject": "$.task.publicId!",
                "note": "Task $.task.title|truncate:80",
                "context": {"field": "$.task.customFields.ref", "limit": 3, "strict": True},
                "empty": None,
            },
            "expect": {"status": "ok", "count": 0, "clean": True, "reason": None},
        },
    ),
    ("deterministic", {"artifact": {"type": "sample-doc"}}),
    (
        "deterministic",
        {
            "artifact": {
                "type": "sample-doc",
                "mediaTypes": ["text/markdown", "image/*"],
                "content": "optional",
            }
        },
    ),
    ("external_state", {}),
    ("external_state", {"event": "event.sample"}),
    ("human", {}),
    ("human", {"approver": PRINCIPAL}),
    ("human", {"approverRole": PRINCIPAL}),
    ("llm_judge", {}),
    ("llm_judge", {"approver": PRINCIPAL, "rubric": "The result answers the request"}),
]


@pytest.mark.parametrize(("kind", "spec"), VALID_SPECS)
def test_each_kind_accepts_its_grammar(kind: str, spec: dict[str, Any]) -> None:
    check = {"key": "c", "kind": kind, "description": "d", "spec": spec}
    assert normalize_checks([check]) == [check]


INVALID_SPECS: list[tuple[str, dict[str, Any], str]] = [
    # deterministic
    ("deterministic", {}, "acceptance[0].spec.skill"),
    ("deterministic", {"skill": "check.sample"}, "acceptance[0].spec.skill"),
    ("deterministic", {"skill": "Check Sample@1"}, "acceptance[0].spec.skill"),
    ("deterministic", {"skill": 7}, "acceptance[0].spec.skill"),
    ("deterministic", {"skill": "check.sample@1", "suite": "all"}, "acceptance[0].spec"),
    ("deterministic", {"skill": "check.sample@1", "inputs": []}, "acceptance[0].spec.inputs"),
    (
        "deterministic",
        {"skill": "check.sample@1", "inputs": {"a": "$.approval.comment"}},
        "acceptance[0].spec.inputs.a",
    ),
    (
        "deterministic",
        {"skill": "check.sample@1", "inputs": {"a": "$.spawnedBy.title"}},
        "acceptance[0].spec.inputs.a",
    ),
    (
        "deterministic",
        {"skill": "check.sample@1", "inputs": {"a": "$.invocation.status"}},
        "acceptance[0].spec.inputs.a",
    ),
    (
        "deterministic",
        {"skill": "check.sample@1", "inputs": {"a": "$.task.nope"}},
        "acceptance[0].spec.inputs.a",
    ),
    (
        "deterministic",
        {"skill": "check.sample@1", "inputs": {"a": {"b": "$.task.nope"}}},
        "acceptance[0].spec.inputs.a.b",
    ),
    (
        "deterministic",
        {"skill": "check.sample@1", "inputs": {"a": [1, 2]}},
        "acceptance[0].spec.inputs.a",
    ),
    ("deterministic", {"skill": "check.sample@1", "expect": {}}, "acceptance[0].spec.expect"),
    (
        "deterministic",
        {"skill": "check.sample@1", "expect": {"status": "$.task.status"}},
        "acceptance[0].spec.expect.status",
    ),
    (
        "deterministic",
        {"skill": "check.sample@1", "expect": {"status": ["ok"]}},
        "acceptance[0].spec.expect.status",
    ),
    (
        "deterministic",
        {"skill": "check.sample@1", "expect": {"not-a-field": "ok"}},
        "acceptance[0].spec.expect.not-a-field",
    ),
    (
        "deterministic",
        {"artifact": {"type": "sample-doc"}, "skill": "check.sample@1"},
        "acceptance[0].spec",
    ),
    (
        "deterministic",
        {"artifact": {"type": "sample-doc"}, "expect": {"ok": True}},
        "acceptance[0].spec",
    ),
    ("deterministic", {"artifact": "sample-doc"}, "acceptance[0].spec.artifact"),
    ("deterministic", {"artifact": {}}, "acceptance[0].spec.artifact.type"),
    ("deterministic", {"artifact": {"type": "Sample Doc"}}, "acceptance[0].spec.artifact.type"),
    (
        "deterministic",
        {"artifact": {"type": "sample-doc", "maxBytes": 1}},
        "acceptance[0].spec.artifact",
    ),
    (
        "deterministic",
        {"artifact": {"type": "sample-doc", "mediaTypes": []}},
        "acceptance[0].spec.artifact.mediaTypes",
    ),
    (
        "deterministic",
        {"artifact": {"type": "sample-doc", "mediaTypes": ["markdown"]}},
        "acceptance[0].spec.artifact.mediaTypes[0]",
    ),
    (
        "deterministic",
        {"artifact": {"type": "sample-doc", "content": "maybe"}},
        "acceptance[0].spec.artifact.content",
    ),
    # external_state
    ("external_state", {"event": ""}, "acceptance[0].spec.event"),
    ("external_state", {"event": "Event Sample"}, "acceptance[0].spec.event"),
    ("external_state", {"event": 1}, "acceptance[0].spec.event"),
    ("external_state", {"skill": "check.sample@1"}, "acceptance[0].spec"),
    # human
    ("human", {"approver": "someone"}, "acceptance[0].spec.approver"),
    ("human", {"approverRole": 5}, "acceptance[0].spec.approverRole"),
    ("human", {"approver": PRINCIPAL, "approverRole": PRINCIPAL}, "acceptance[0].spec"),
    ("human", {"rubric": "a model may not decide this"}, "acceptance[0].spec"),
    # llm_judge
    ("llm_judge", {"rubric": " "}, "acceptance[0].spec.rubric"),
    ("llm_judge", {"rubric": "x" * 2001}, "acceptance[0].spec.rubric"),
    ("llm_judge", {"model": "any"}, "acceptance[0].spec"),
    ("llm_judge", {"approver": "someone"}, "acceptance[0].spec.approver"),
]


@pytest.mark.parametrize(("kind", "spec", "field"), INVALID_SPECS)
def test_each_kind_refuses_a_spec_outside_its_grammar(
    kind: str, spec: dict[str, Any], field: str
) -> None:
    check = {"key": "c", "kind": kind, "description": "d", "spec": spec}
    with pytest.raises(ValidationError) as caught:
        normalize_checks([check])
    assert caught.value.code == INVALID_ACCEPTANCE_SPEC
    assert caught.value.details == {"field": field, "kind": kind}


def test_every_kind_has_a_grammar_and_a_refusal() -> None:
    assert set(SPEC_KEYS) == CHECK_KINDS
    assert {kind for kind, _, _ in INVALID_SPECS} == CHECK_KINDS
    assert {kind for kind, _ in VALID_SPECS} == CHECK_KINDS


def test_the_output_prefix_is_reserved_in_a_task_acceptance_only() -> None:
    check = {"key": "output.plan", "kind": "human", "description": "d"}
    with pytest.raises(ValidationError) as caught:
        normalize_checks([check])
    assert caught.value.code == "invalid_acceptance"
    assert caught.value.details["field"] == "acceptance[0].key"
    # Goal criteria are not verified: nothing is reserved there.
    assert normalize_checks([check], field="criteria", typed_spec=False) == [check]


def test_required_outputs_become_artifact_checks_in_declared_order() -> None:
    schema = parse_artifact_schema(
        {
            "outputs": [
                {"key": "draft", "type": "sample-doc", "required": False},
                {"key": "plan", "type": "sample-doc", "required": True},
                {
                    "key": "scan",
                    "type": "sample-scan",
                    "required": True,
                    "mediaTypes": ["Image/PNG"],
                    "content": "optional",
                },
            ]
        }
    )
    checks = output_checks(schema)
    assert checks == [
        {
            "key": "output.plan",
            "kind": "deterministic",
            "description": "Required output plan (sample-doc)",
            "spec": {"artifact": {"type": "sample-doc", "content": "required"}},
        },
        {
            "key": "output.scan",
            "kind": "deterministic",
            "description": "Required output scan (sample-scan)",
            "spec": {
                "artifact": {
                    "type": "sample-scan",
                    "mediaTypes": ["image/png"],
                    "content": "optional",
                }
            },
        },
    ]
    # Their form is the grammar's own, only the key prefix is not a task's.
    for check in checks:
        check_spec(check["kind"], check["spec"])
    assert output_checks(parse_artifact_schema({})) == []


@pytest.mark.parametrize("kind", sorted(CHECK_KINDS))
def test_a_check_without_spec_is_accepted_as_before(kind: str) -> None:
    check = {"key": "c", "kind": kind, "description": "d"}
    assert normalize_checks([check]) == [check]


def test_secrets_and_size_are_refused_before_the_grammar() -> None:
    check = {"key": "c", "kind": "llm_judge", "description": "d", "spec": {"apiToken": "x"}}
    assert _code(normalize_checks, [check]) == "secret_material_rejected"


def test_goal_criteria_keep_an_opaque_spec() -> None:
    criterion = {"key": "c", "kind": "deterministic", "description": "d", "spec": {"suite": "x"}}
    assert normalize_checks([criterion], field="criteria", typed_spec=False) == [criterion]
    assert _code(normalize_checks, [criterion], field="criteria") == INVALID_ACCEPTANCE_SPEC


def test_order_advice_is_advice_not_an_error() -> None:
    checks = normalize_checks(
        [
            {"key": "a", "kind": "human", "description": "d"},
            {"key": "b", "kind": "deterministic", "description": "d"},
            {"key": "c", "kind": "external_state", "description": "d"},
            {"key": "e", "kind": "llm_judge", "description": "d"},
        ]
    )
    advice = check_order_advice(checks)
    assert len(advice) == 2
    assert "'b'" in advice[0] and "'c'" in advice[1]
    assert check_order_advice(checks[1:]) == []


def test_evidence_may_only_cite_declared_checks() -> None:
    acceptance = normalize_checks([{"key": "a", "kind": "human", "description": "ok"}])
    fine = normalize_evidence([{"kind": "artifact", "artifactId": ART, "check": "a"}])
    stray = normalize_evidence([{"kind": "artifact", "artifactId": ART, "check": "b"}])
    check_evidence_against_acceptance(fine, acceptance)
    assert _code(check_evidence_against_acceptance, stray, acceptance) == "unknown_acceptance_check"


def test_origin_summary_carries_references_not_notes() -> None:
    origin = normalize_origin(
        {
            "kind": "rule",
            "ruleId": "drift",
            "evidence": [
                {"kind": "observation", "observationId": OBS, "note": "long story"},
                {
                    "kind": "external",
                    "externalRef": {"system": "vcs", "id": "abc", "url": "https://x/abc"},
                },
            ],
        }
    )
    assert origin_summary(origin) == {
        "kind": "rule",
        "ruleId": "drift",
        "evidence": [
            {"kind": "observation", "observationId": OBS},
            {"kind": "external", "externalRef": {"system": "vcs", "id": "abc"}},
        ],
    }


def test_work_graph_events_reach_memory_with_goal_scopes() -> None:
    goal_id = str(uuid.uuid4())
    parent_id = str(uuid.uuid4())
    task = map_event(
        _event(
            "task.created",
            {
                "publicId": "TASK-1",
                "title": "Restore",
                "goalId": goal_id,
                "origin": {"kind": "rule", "ruleId": "drift", "evidence": []},
                "acceptanceChecks": 2,
            },
        )
    )
    assert task is not None
    assert task["data"]["goalId"] == goal_id
    assert task["data"]["origin"]["ruleId"] == "drift"
    assert "acceptanceChecks" not in task["data"]
    assert {"type": "goal", "id": goal_id} in task["scopes"]

    goal = map_event(
        _event(
            "goal.created",
            {"goalId": goal_id, "title": "Hold", "parentGoalId": parent_id, "criteriaCount": 1},
            entity_type="goal",
        )
    )
    assert goal is not None
    assert goal["subject"]["type"] == "goal"
    assert goal["content"] == "goal.created: Hold"
    assert {"type": "goal", "id": parent_id} in goal["scopes"]
    assert map_event(_event("goal.updated", {"changes": {"desired_state": True}})) is not None


def test_context_pack_is_a_pointer_like_any_other_fact() -> None:
    pack = str(uuid.uuid4())
    items = normalize_evidence(
        [{"kind": "context_pack", "contextPackId": pack.upper(), "check": "contracts-known"}]
    )
    assert items == [{"kind": "context_pack", "contextPackId": pack, "check": "contracts-known"}]
    assert context_pack_targets(items) == {uuid.UUID(pack)}
    assert evidence_targets(items) == (set(), set())
    assert _code(normalize_evidence, [{"kind": "context_pack"}]) == "invalid_evidence"
    assert (
        _code(normalize_evidence, [{"kind": "context_pack", "contextPackId": pack}] * 2)
        == "duplicate_evidence"
    )
    summary = origin_summary({"kind": "harness", "evidence": items})
    assert summary["evidence"] == [{"kind": "context_pack", "contextPackId": pack}]
