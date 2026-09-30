"""The agent registry contract (CP-ADR-0073).

What is pinned here: the ``/agents`` routes and bodies in OpenAPI, the core's
validator ``AgentSpec`` against the catalog schema of kind ``Agent`` of the
superproject (``$defs.agentSpec``, declarative-agents D001), and the
``agent.*`` payloads of the event catalog.

The examples in ``tests/fixtures/agents`` are the examples of the superproject
(``tools/tests/test_agent_schema.py``): one per executor kind plus an identity
without placement. The catalog schema is read from the superproject when this
repository is checked out inside it, and from the pinned copy
``tests/fixtures/superproject/object.schema.json`` otherwise.
"""

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    AGENT_INSTRUCTIONS_MAX_CHARS,
    AgentPublishRequest,
    AgentSpec,
    AgentStateUpdateRequest,
    AgentStatusReport,
)
from control_plane.application.commands.agents import spec_hash_of, split_desired_state
from control_plane.domain.enums import AgentPhase, AgentState, Permission
from control_plane.domain.event_catalog import get_event_type

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AGENTS = FIXTURES / "agents"
PINNED_SCHEMA = FIXTURES / "superproject" / "object.schema.json"
# In the superproject control-plane is a submodule at its root (flat layout).
SUPERPROJECT_SCHEMA = (
    Path(__file__).resolve().parents[3] / "packages" / "schema" / "v1" / "object.schema.json"
)
# What the kind Agent is made of in the catalog schema.
AGENT_DEFS = (
    "agentSpec",
    "agentExecutors",
    "displayName",
    "slug",
    "typeKey",
    "permission",
    "envOrUuid",
    "nodeLabel",
    "secretName",
)


def _objects() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(AGENTS.glob("*.yaml"))
    ]


def _object(key: str) -> dict[str, Any]:
    return yaml.safe_load((AGENTS / f"{key}.yaml").read_text(encoding="utf-8"))


def _catalog_schema() -> dict[str, Any]:
    path = SUPERPROJECT_SCHEMA if SUPERPROJECT_SCHEMA.is_file() else PINNED_SCHEMA
    return json.loads(path.read_text(encoding="utf-8"))


CATALOG = jsonschema.Draft202012Validator(_catalog_schema())
API_VERSION = _object("coder")["apiVersion"]


def _catalog_errors(key: str, spec: dict[str, Any]) -> list[str]:
    document = {"apiVersion": API_VERSION, "kind": "Agent", "key": key, "spec": spec}
    return [error.message for error in CATALOG.iter_errors(document)]


def _core_accepts(key: str, spec: dict[str, Any]) -> bool:
    try:
        AgentPublishRequest.model_validate({"key": key, "spec": spec})
    except ValidationError:
        return False
    return True


def _set(spec: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    target = spec
    for part in path[:-1]:
        target = target.setdefault(part, {})
    if value is DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return spec


DELETE = object()


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


def _ref(schema: dict[str, Any]) -> str:
    return schema["$ref"].rsplit("/", 1)[-1]


# --- OpenAPI -----------------------------------------------------------------

ROUTES: dict[tuple[str, str], tuple[str | None, str]] = {
    ("post", "/api/v1/agents"): ("AgentPublishRequest", "AgentOut"),
    ("post", "/api/v1/agents:validate"): ("AgentPublishRequest", "AgentValidationOut"),
    ("get", "/api/v1/agents"): (None, "AgentPageOut"),
    ("get", "/api/v1/agents/me"): (None, "AgentOut"),
    ("get", "/api/v1/agents/{ref}"): (None, "AgentOut"),
    ("patch", "/api/v1/agents/{key}/state"): ("AgentStateUpdateRequest", "AgentOut"),
    ("post", "/api/v1/agents/{key}:retire"): ("AgentRetireRequest", "AgentOut"),
    ("put", "/api/v1/agents/{key}/identity"): ("AgentIdentityLinkRequest", "AgentOut"),
    ("post", "/api/v1/agents/{key}/identity:replace"): ("AgentIdentityReplaceRequest", "AgentOut"),
    ("get", "/api/v1/agents/{key}/revisions"): (None, "AgentRevisionPageOut"),
    ("get", "/api/v1/agents/{key}/status"): (None, "AgentStatusOut"),
    ("put", "/api/v1/agents/{key}/status"): ("AgentStatusReport", "AgentStatusOut"),
}


def test_openapi_carries_every_agents_route_with_its_bodies() -> None:
    paths = _openapi()["paths"]
    published = {
        (method, path) for path, item in paths.items() if "/agents" in path for method in item
    }
    assert published == set(ROUTES)
    for (method, path), (request, response) in ROUTES.items():
        operation = paths[path][method]
        body = operation.get("requestBody")
        if request is None:
            assert body is None, (method, path)
        else:
            assert _ref(body["content"]["application/json"]["schema"]) == request
        success = "201" if (method, path) == ("post", "/api/v1/agents") else "200"
        assert _ref(operation["responses"][success]["content"]["application/json"]["schema"]) == (
            response
        )
        assert "501" not in operation["responses"], "implemented routes no longer answer 501"


def test_openapi_carries_the_revision_of_a_run() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert "agentRevisionId" in schemas["RunOut"]["properties"]
    assert "agentRevisionId" in schemas["RunStartRequest"]["properties"]
    assert "agentRevisionId" not in schemas["RunStartRequest"].get("required", [])


def test_openapi_lists_runs_by_executor() -> None:
    """``GET /runs?principalId=&agentKey=`` (amendment of 2026-09-29, Г1)."""
    operation = _openapi()["paths"]["/api/v1/runs"]["get"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert {"taskId", "claimId", "status", "limit", "cursor"} <= set(params)
    assert params["principalId"]["schema"]["anyOf"][0] == {"type": "string", "format": "uuid"}
    assert params["agentKey"]["schema"]["anyOf"][0] == {"type": "string"}
    assert not params["principalId"]["required"] and not params["agentKey"]["required"]
    assert "startedAt" in operation["description"]


def test_openapi_has_no_harness_manifests() -> None:
    """The agent revision replaces the manifest of CP-ADR-0043 (§ Consequences, D007)."""
    openapi = _openapi()
    assert [path for path in openapi["paths"] if "manifest" in path.lower()] == []
    assert [name for name in openapi["components"]["schemas"] if "Manifest" in name] == []


def test_permissions_are_in_the_catalog() -> None:
    assert {p.value for p in Permission} >= {
        "agents.read",
        "agents.manage",
        "agents.status.write",
    }
    catalog = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "authz" / "catalog.yaml").read_text("utf-8")
    )
    for name in ("agents.read", "agents.manage", "agents.status.write"):
        assert catalog["actions"][name] == {"resource": "tenant"}


# --- the core's validator against the catalog schema of the kind -------------


def test_the_pinned_schema_is_the_superproject_one() -> None:
    """The whole copy, not only the kind Agent: the kinds TaskType and WorkRule
    carry the core's request bodies too (declarative-cycle C001/C002)."""
    if not SUPERPROJECT_SCHEMA.is_file():
        pytest.skip("not checked out inside the superproject")
    live = json.loads(SUPERPROJECT_SCHEMA.read_text(encoding="utf-8"))
    pinned = json.loads(PINNED_SCHEMA.read_text(encoding="utf-8"))
    assert {name: pinned["$defs"][name] for name in AGENT_DEFS} == {
        name: live["$defs"][name] for name in AGENT_DEFS
    }
    assert pinned == live


def test_the_examples_cover_every_executor_kind_and_the_catalog_accepts_them() -> None:
    kinds = set()
    for document in _objects():
        assert document["kind"] == "Agent"
        assert _catalog_errors(document["key"], document["spec"]) == [], document["key"]
        spec = document["spec"]
        kinds.add("none" if spec.get("placement") == "none" else spec["executor"]["kind"])
    assert kinds == {"claude-code", "codex", "skills", "none"}


@pytest.mark.parametrize("key", ["coder", "reviewer", "skills-executor", "process-bridge"])
def test_every_catalog_example_passes_the_core_and_comes_back_as_it_was(key: str) -> None:
    """SC-007 at the core's level: nothing is added or renamed on the way in."""
    document = _object(key)
    request = AgentPublishRequest.model_validate({"key": key, "spec": document["spec"]})
    assert (
        request.spec.model_dump(mode="json", by_alias=True, exclude_unset=True)
        == (document["spec"])
    )


# (example, path in spec, value): documents the catalog schema accepts.
BOTH_ACCEPT: list[tuple[str, tuple[str, ...], Any]] = [
    ("skills-executor", ("placement",), DELETE),  # absent: placed with the defaults
    ("coder", ("placement", "requires"), ["gpu", "gpu.model=a100", "zone=eu-1"]),
    ("coder", ("placement", "drainSeconds"), 0),
    ("coder", ("placement", "resources"), {"cpus": 0.5}),
    ("coder", ("work", "includeSubprojects"), True),
    ("coder", ("work", "project"), "00000000-0000-4000-8000-000000000001"),
    ("coder", ("skills", "mcpOrigins"), ["https://mcp.example.com"]),
    ("coder", ("skills", "audiences"), ["platform-core"]),
    ("skills-executor", ("skills", "concurrency"), 32),
    ("skills-executor", ("skills", "invoke"), ["oss.publish@1", "ledger.post@2.0.0"]),
    ("coder", ("workingCopy", "baseRef"), "feature/declarative-agents"),
    ("coder", ("workingCopy", "superproject"), "https://git.example/org/superproject.git"),
    ("coder", ("workingCopy", "publish"), False),
    ("coder", ("workingCopy", "review", "taskTypes"), ["coding-task"]),
    ("coder", ("workingCopy", "review", "base"), "main"),
    ("process-bridge", ("executor",), {"kind": "skills"}),
    ("process-bridge", ("description",), "Bridges process-runtime to the queue"),
    ("process-bridge", ("identity", "iam"), DELETE),
    ("coder", ("identity", "iam"), {"audiences": ["iam"], "scopeCeiling": ["iam:agents"]}),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["policy:check-on-behalf"]),
]


@pytest.mark.parametrize(("key", "path", "value"), BOTH_ACCEPT)
def test_what_the_catalog_accepts_the_core_accepts_unchanged(
    key: str, path: tuple[str, ...], value: Any
) -> None:
    spec = _set(copy.deepcopy(_object(key)["spec"]), path, value)
    assert _catalog_errors(key, spec) == []
    parsed = AgentSpec.model_validate(spec)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == spec


# (example, path in spec, value): documents the catalog schema rejects.
BOTH_REJECT: list[tuple[str, tuple[str, ...], Any]] = [
    ("process-bridge", ("placement",), {"replicas": 1}),  # placed without executor
    ("process-bridge", ("placement",), DELETE),  # placed by default, without executor
    ("coder", ("workspace",), {"repository": "https://git.example/org/control-plane.git"}),
    ("coder", ("workingCopy", "repository"), DELETE),
    ("coder", ("workingCopy", "directory"), "Control Plane"),
    ("coder", ("workingCopy", "review", "mode"), "later"),
    ("coder", ("workingCopy", "review", "unknownField"), True),
    ("coder", ("skills", "concurrency"), 0),
    ("coder", ("skills", "concurrency"), 33),
    ("coder", ("skills", "local"), ["not an entry point"]),
    ("coder", ("skills", "unknownField"), True),
    ("coder", ("skills", "invoke"), ["oss.publish"]),  # a version is pinned
    ("coder", ("skills", "invoke"), ["oss.publish@1", "oss.publish@1"]),
    ("coder", ("skills", "invoke"), ["oss publish@1"]),
    ("coder", ("skills", "invoke"), ["@1"]),
    ("coder", ("placement", "requires"), ["GPU"]),
    ("coder", ("placement", "requires"), ["gpu=a 100"]),
    ("coder", ("placement", "secrets"), ["sk-ant-Very_Secret"]),
    ("coder", ("placement", "secrets"), ["claude.oauth"]),
    ("coder", ("placement", "replicas"), 21),
    ("coder", ("placement", "replicas"), -1),
    ("coder", ("placement", "drainSeconds"), 14_401),
    ("coder", ("placement", "resources"), {"gpus": 1}),
    ("coder", ("placement", "resources"), {"memoryMb": 32}),
    ("coder", ("work", "unknownField"), True),
    ("coder", ("identity", "kind"), DELETE),
    ("coder", ("identity", "kind"), "human"),
    ("coder", ("identity", "unknownField"), True),
    ("coder", ("identity", "permissions"), ["Tasks Read"]),
    ("process-bridge", ("identity", "iam", "unknownField"), True),
    ("process-bridge", ("identity", "iam", "audiences"), DELETE),
    ("process-bridge", ("identity", "iam", "audiences"), []),
    ("process-bridge", ("identity", "iam", "audiences"), ["a", "a"]),
    ("process-bridge", ("identity", "iam", "audiences"), [f"a{i}" for i in range(21)]),
    ("process-bridge", ("identity", "iam", "audiences"), [""]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), DELETE),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), []),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["read"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["Control-Plane:read"]),
    ("process-bridge", ("identity", "iam", "scopeCeiling"), ["memory:read", "memory:read"]),
    (
        "process-bridge",
        ("identity", "iam", "scopeCeiling"),
        [f"control-plane:s{i}" for i in range(51)],
    ),
    ("coder", ("state",), "paused"),
    ("coder", ("unknownTopLevel",), 1),
    ("coder", ("description",), "x" * 2001),
]


@pytest.mark.parametrize(("key", "path", "value"), BOTH_REJECT)
def test_what_the_catalog_rejects_the_core_rejects(
    key: str, path: tuple[str, ...], value: Any
) -> None:
    spec = _set(copy.deepcopy(_object(key)["spec"]), path, value)
    assert _catalog_errors(key, spec) != []
    assert not _core_accepts(key, spec)


def _hash_as_published(spec: dict[str, Any]) -> str:
    sent = AgentSpec.model_validate(spec).model_dump(mode="json", by_alias=True, exclude_unset=True)
    body, _, _ = split_desired_state(sent)
    return spec_hash_of(body)[1]


def test_identity_iam_is_part_of_the_revision_and_absent_iam_hashes_as_before() -> None:
    """D014a: ``identity.iam`` is data of the spec; without it the hash is what it was."""
    with_iam = _object("process-bridge")["spec"]
    without_iam = copy.deepcopy(with_iam)
    del without_iam["identity"]["iam"]
    assert (
        "iam"
        not in AgentSpec.model_validate(without_iam).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )["identity"]
    )
    # The hash of a spec without iam is the hash of the same dict before D014a.
    assert _hash_as_published(without_iam) == spec_hash_of(split_desired_state(without_iam)[0])[1]
    assert _hash_as_published(with_iam) != _hash_as_published(without_iam)


def test_executor_parameters_are_not_interpreted() -> None:
    spec = _object("coder")["spec"]
    spec["executor"]["params"] = {"anything": {"nested": [1, "two"]}}
    assert AgentSpec.model_validate(spec).executor.params == {"anything": {"nested": [1, "two"]}}  # type: ignore[union-attr]


def test_the_core_is_stricter_where_the_adr_says_so() -> None:
    """CP-ADR-0073 §1: an agent without permissions could not even read its work."""
    spec = _object("coder")["spec"]
    spec["identity"]["permissions"] = []
    assert _catalog_errors("coder", spec) == []
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(spec)


def test_instructions_are_not_bound_by_the_short_string_limit() -> None:
    spec = _object("coder")["spec"]
    spec["executor"]["instructions"] = "x" * AGENT_INSTRUCTIONS_MAX_CHARS
    AgentSpec.model_validate(spec)
    spec["executor"]["instructions"] += "x"
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(spec)


def test_key_is_a_dns_label() -> None:
    spec = _object("coder")["spec"]
    for bad in ("Coder", "coder_1", "-coder", "c" * 64, "coder@2"):
        with pytest.raises(ValidationError):
            AgentPublishRequest.model_validate({"key": bad, "spec": spec})


def test_state_update_needs_something_to_change() -> None:
    with pytest.raises(ValidationError):
        AgentStateUpdateRequest.model_validate({})
    assert AgentStateUpdateRequest.model_validate({"replicas": 0}).replicas == 0


def test_status_report_speaks_the_phases_of_the_domain() -> None:
    schema = AgentStatusReport.model_json_schema(by_alias=True)
    assert set(schema["properties"]["phase"]["enum"]) == {p.value for p in AgentPhase}
    with pytest.raises(ValidationError):  # observedAt without a zone is ambiguous
        AgentStatusReport.model_validate(
            {
                "phase": "running",
                "instances": {"desired": 1, "ready": 1},
                "observedAt": "2026-09-27T10:00:00",
            }
        )


def test_desired_states_match_the_domain() -> None:
    schema = AgentSpec.model_json_schema(by_alias=True)
    assert set(schema["properties"]["state"]["enum"]) == {s.value for s in AgentState}


# --- events ------------------------------------------------------------------

SAMPLES: dict[str, dict[str, Any]] = {
    "agent.revision_published": {
        "key": "selfdev-coder",
        "revision": 2,
        "specHash": "sha256:" + "a" * 64,
        "previousRevision": 1,
        "executorKind": "claude-code",
        "placed": True,
        "permissionsChanged": False,
    },
    "agent.state_changed": {
        "key": "selfdev-coder",
        "state": "stopped",
        "replicas": 1,
        "previousState": "running",
        "previousReplicas": 1,
    },
    "agent.status_changed": {
        "key": "selfdev-coder",
        "phase": "waiting_for_node",
        "previousPhase": None,
        "reasonCode": "no_matching_node",
        "node": None,
        "observedRevision": None,
        "observedAt": "2026-09-27T10:00:00+00:00",
    },
    "agent.retired": {
        "key": "selfdev-coder",
        "revision": 3,
        "principalId": None,
        "reason": "replaced by selfdev-coder-2",
        "releasedClaims": 0,
    },
}


@pytest.mark.parametrize("event_type", sorted(SAMPLES))
def test_agent_events_are_in_the_catalog(event_type: str) -> None:
    entry = get_event_type(event_type)
    assert entry.entity_type == "agent"
    schema = entry.current.schema
    jsonschema.validate(SAMPLES[event_type], schema)
    assert set(schema["required"]) == set(SAMPLES[event_type])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**SAMPLES[event_type], "key": None}, schema)
