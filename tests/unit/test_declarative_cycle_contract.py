"""The declarative-cycle contract in OpenAPI (C002).

Amendments of 2026-09-27: CP-ADR-0067 (B5-B7: default acceptance of a task
type, ``when`` of a check), CP-ADR-0063 (G1: ``identity`` of a rule) and
CP-ADR-0073 (A1: ``agent:<key>`` in an assignment field). What is pinned here
is the published shape. The acceptance of a task type and ``when`` are
implemented (C004): the grammar of a check takes ``when`` here too; the
identity of a rule is implemented (C005,
``tests/integration/test_rules_identity_relations.py``) and an ``agent:<key>``
assignee is resolved (C006, ``tests/integration/test_agent_assignees.py``):
no route answers 501 any more.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    AcceptanceCheckSpec,
    AgentSpec,
    RuleCreateRequest,
    RuleIdentitySpec,
    TaskCreateRequest,
    TaskTypeCreateRequest,
)
from control_plane.domain.errors import DomainError
from control_plane.domain.work_graph import normalize_checks
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


OPENAPI = _openapi()
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]


def _ref(schema: dict[str, Any]) -> str:
    return str(schema["$ref"]).rsplit("/", 1)[-1]


def _refs(schema: dict[str, Any]) -> set[str]:
    return {_ref(option) for option in schema.get("anyOf", [schema]) if "$ref" in option}


def test_a_check_carries_when() -> None:
    when = SCHEMAS["AcceptanceCheckSpec"]["properties"]["when"]
    array = next(option for option in when["anyOf"] if option.get("type") == "array")
    assert (array["minItems"], array["maxItems"]) == (1, 8)
    assert array["items"]["type"] == "string"
    assert "when" not in SCHEMAS["AcceptanceCheckSpec"].get("required", [])


def test_a_task_type_version_carries_default_acceptance() -> None:
    request = SCHEMAS["TaskTypeCreateRequest"]["properties"]["acceptance"]
    assert _ref(request["items"]) == "AcceptanceCheckSpec"
    assert request["maxItems"] == 50
    assert "acceptance" not in SCHEMAS["TaskTypeCreateRequest"].get("required", [])
    assert SCHEMAS["TaskTypeOut"]["properties"]["acceptance"]["type"] == "array"


@pytest.mark.parametrize("name", ["RuleCreateRequest", "RuleUpdateRequest"])
def test_a_rule_document_carries_identity(name: str) -> None:
    identity = SCHEMAS[name]["properties"]["identity"]
    assert _refs(identity) == {"RuleIdentitySpec"}
    assert "identity" not in SCHEMAS[name].get("required", [])
    assert SCHEMAS["RuleIdentitySpec"]["required"] == ["agent"]
    assert "identity" in SCHEMAS["RuleOut"]["properties"]


@pytest.mark.parametrize("name", ["TaskCreateRequest", "TaskUpdateRequest"])
def test_an_assignee_is_a_principal_id_or_an_agent_reference(name: str) -> None:
    options = SCHEMAS[name]["properties"]["assigneeId"]["anyOf"]
    assert {"type": "string", "format": "uuid"} in options
    assert {"type": "string", "pattern": "^agent:[a-z0-9][a-z0-9-]{0,62}$"} in options


@pytest.mark.parametrize(
    ("method", "path"), [("post", "/api/v1/tasks"), ("patch", "/api/v1/tasks/{task_ref}")]
)
def test_task_routes_resolve_the_assignee_and_document_no_501(method: str, path: str) -> None:
    responses = OPENAPI["paths"][path][method]["responses"]
    assert "501" not in responses
    assert "422" in responses


def test_an_agent_reference_is_parsed_apart_from_a_principal_id() -> None:
    principal = uuid.uuid4()
    assert TaskCreateRequest.model_validate(
        {"title": "t", "assigneeId": str(principal)}
    ).assignee_id == (principal)
    assert (
        TaskCreateRequest.model_validate({"title": "t", "assigneeId": "agent:coder"}).assignee_id
        == "agent:coder"
    )
    for bad in ("agent:", "agent:Coder", "coder", "agent:-coder", "agent:" + "c" * 64):
        with pytest.raises(ValidationError):
            TaskCreateRequest.model_validate({"title": "t", "assigneeId": bad})


# --- the catalog schema of the superproject (C001) against the core ---------
#
# The kinds TaskType and WorkRule of the catalog carry what the core takes in
# POST /task-types and POST /rules. What the catalog accepts for an amended
# field, the core's request model accepts; what it rejects, the core rejects.
# The schema is read from the superproject when this repository is checked out
# inside it, from the pinned copy otherwise (as in test_agent_contract.py).

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PINNED_SCHEMA = FIXTURES / "superproject" / "object.schema.json"
SUPERPROJECT_SCHEMA = (
    Path(__file__).resolve().parents[3] / "packages" / "schema" / "v1" / "object.schema.json"
)
CATALOG_SCHEMA: dict[str, Any] = json.loads(
    (SUPERPROJECT_SCHEMA if SUPERPROJECT_SCHEMA.is_file() else PINNED_SCHEMA).read_text("utf-8")
)
CATALOG = jsonschema.Draft202012Validator(CATALOG_SCHEMA)
API_VERSION = CATALOG_SCHEMA["properties"]["apiVersion"]["const"]


def _catalog_errors(kind: str, key: str, spec: dict[str, Any]) -> list[str]:
    document = {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec}
    return [error.message for error in CATALOG.iter_errors(document)]


def _core_accepts(model: type[BaseModel], value: Any) -> bool:
    try:
        model.model_validate(value)
    except ValidationError:
        return False
    return True


def _task_type(*checks: dict[str, Any]) -> dict[str, Any]:
    return {
        "displayName": "Sample work",
        "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
        "acceptance": list(checks),
    }


def _core_accepts_check(check: dict[str, Any]) -> bool:
    """The request model, then the grammar of a task's acceptance, ``when`` included."""
    if not _core_accepts(AcceptanceCheckSpec, check):
        return False
    try:
        normalize_checks([check], typed_spec=True)
    except DomainError:
        return False
    return True


def _rule(**extra: Any) -> dict[str, Any]:
    return {
        "trigger": {"kind": "observation", "type": "sample.appeared"},
        "action": {"kind": "ensure_work", "taskType": "task"},
        **extra,
    }


REVIEW = {"key": "review", "kind": "human", "description": "A person approves the work"}
MERGE = {
    "key": "merge",
    "kind": "deterministic",
    "description": "The branch is merged",
    "spec": {"skill": "git.merge@1"},
    "when": ["$.task.customFields.branch"],
}

CHECKS_BOTH_ACCEPT: list[dict[str, Any]] = [
    REVIEW,
    MERGE,
    {**REVIEW, "when": ["$.task.customFields.branch", "$.task.artifact[commit].metadata.sha"]},
    {**REVIEW, "when": [f"$.task.customFields.f{i}" for i in range(8)]},
    {**REVIEW, "kind": "llm_judge", "spec": {"rubric": "Is it done?"}},
    {**REVIEW, "kind": "external_state"},
    {**REVIEW, "key": "ci.green_1-a"},
]

CHECKS_BOTH_REJECT: list[dict[str, Any]] = [
    {**REVIEW, "when": []},
    {**REVIEW, "when": [f"$.task.customFields.f{i}" for i in range(9)]},
    {**REVIEW, "when": [""]},
    {**REVIEW, "when": "$.task.customFields.branch"},
    {**REVIEW, "kind": "manual"},
    {**REVIEW, "key": "Review"},
    {**REVIEW, "key": ""},
    {**REVIEW, "description": "x" * 2001},
    {**REVIEW, "unknownField": True},
    {key: value for key, value in REVIEW.items() if key != "kind"},
    {key: value for key, value in REVIEW.items() if key != "key"},
    # Reconciled in the superproject after C002 (2abb115): the catalog, like
    # the core, requires a description and keys of at most 63 characters.
    {key: value for key, value in REVIEW.items() if key != "description"},
    {**REVIEW, "key": "r" * 64},
]


def test_the_catalog_carries_the_amended_fields() -> None:
    defs = CATALOG_SCHEMA["$defs"]
    acceptance = defs["taskTypeSpec"]["properties"]["acceptance"]
    assert _ref(acceptance["items"]) == "acceptanceCriterion"
    criterion = defs["acceptanceCriterion"]
    assert set(criterion["properties"]) == {"key", "kind", "description", "spec", "when"}
    assert set(criterion["properties"]["kind"]["enum"]) == set(
        AcceptanceCheckSpec.model_json_schema()["properties"]["kind"]["enum"]
    )
    rule = defs["workRuleSpec"]["properties"]
    assert rule["identity"]["required"] == ["agent"]
    assert set(rule["identity"]["properties"]) == {"agent"}
    action = rule["action"]["properties"]
    assert _ref(action["taskTypes"]["items"]) == "typeKey"
    assert set(action["fields"]["properties"]["relations"]["properties"]) == {
        "spawnedBy",
        "dependsOn",
    }


@pytest.mark.parametrize("check", CHECKS_BOTH_ACCEPT)
def test_a_check_the_catalog_accepts_on_a_task_type_the_core_accepts(
    check: dict[str, Any],
) -> None:
    assert _catalog_errors("TaskType", "sample-work", _task_type(check)) == []
    request = TaskTypeCreateRequest.model_validate({"key": "sample-work", **_task_type(check)})
    assert request.acceptance[0].model_dump(mode="json", exclude_unset=True) == check
    assert _core_accepts_check(check)


@pytest.mark.parametrize("check", CHECKS_BOTH_REJECT)
def test_a_check_the_catalog_rejects_on_a_task_type_the_core_rejects(
    check: dict[str, Any],
) -> None:
    assert _catalog_errors("TaskType", "sample-work", _task_type(check)) != []
    assert not _core_accepts_check(check)


@pytest.mark.parametrize(
    ("identity", "accepted"),
    [
        ({"agent": "rules"}, True),
        ({"agent": "selfdev-coder-2"}, True),
        ({"agent": "c" * 63}, True),
        ({}, False),
        ({"agent": "Rules"}, False),
        ({"agent": "-rules"}, False),
        ({"agent": "rules_1"}, False),
        ({"agent": "c" * 64}, False),
        ({"agent": "rules", "permissions": ["tasks.write"]}, False),
    ],
)
def test_a_rule_identity_means_the_same_to_the_catalog_and_the_core(
    identity: dict[str, Any], accepted: bool
) -> None:
    assert (_catalog_errors("WorkRule", "sample-appeared", _rule(identity=identity)) == []) is (
        accepted
    )
    assert _core_accepts(RuleIdentitySpec, identity) is accepted
    request = {"key": "sample-appeared", **_rule(identity=identity)}
    assert _core_accepts(RuleCreateRequest, request) is accepted


@pytest.mark.parametrize("depends_on", ["{{item.dependsOn}}", ["feature:{{item.dependsOn}}"]])
def test_the_catalog_takes_a_per_item_task_type_and_relations_the_core_takes_later(
    depends_on: str | list[str],
) -> None:
    """Fields of ``action``, which OpenAPI publishes as an object without a
    schema: the request passes, the rule grammar refuses them until C005.
    ``dependsOn`` is a template or a list of them (CP-ADR-0063 G3)."""
    rule = _rule()
    rule["action"] = {
        "kind": "ensure_work",
        "taskType": "{{item.type}}",
        "taskTypes": ["coding-task", "design-task"],
        "fields": {
            "title": "{{item.title}}",
            "relations": {
                "spawnedBy": "{{payload.data.taskId}}",
                "dependsOn": depends_on,
            },
        },
    }
    assert _catalog_errors("WorkRule", "tasks-filed", rule) == []
    assert _core_accepts(RuleCreateRequest, {"key": "tasks-filed", **rule})
    rule["action"]["fields"]["relations"]["blocks"] = ["feature:x"]
    assert _catalog_errors("WorkRule", "tasks-filed", rule) != []


def test_a_git_connector_agent_passes_the_core_unchanged() -> None:
    spec = {
        "displayName": "Git connector",
        "identity": {"kind": "service", "permissions": ["observations.write"]},
        "executor": {
            "kind": "git-connector",
            "params": {
                "repositories": [
                    {"name": "control-plane", "url": "https://git.example/org/cp.git"}
                ],
                "observe": ["commits", "adrRegistry"],
            },
        },
    }
    assert _catalog_errors("Agent", "git-connector", spec) == []
    parsed = AgentSpec.model_validate(spec)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == spec
    del spec["executor"]["params"]
    assert _catalog_errors("Agent", "git-connector", spec) != []
