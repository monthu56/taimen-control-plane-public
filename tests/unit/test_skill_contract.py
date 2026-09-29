"""Skill contract v1 validation (ADR-0056 §1) — pure domain checks."""

import pytest

from control_plane.domain.errors import ValidationError
from control_plane.domain.skill_contract import (
    normalize_contract,
    replace_endpoint,
    schema_errors,
    validate_policy_columns,
)

BASE = {"inputs": {"type": "object"}, "outputs": {"type": "object"}}


def test_mcp_implementation_needs_a_tool_name() -> None:
    normalized = normalize_contract(
        {**BASE, "implementation": {"protocol": "mcp", "endpoint": "github", "entrypoint": "x.y"}}
    )
    assert normalized["implementation"]["entrypoint"] == "x.y"
    with pytest.raises(ValidationError) as exc:
        normalize_contract({**BASE, "implementation": {"protocol": "mcp"}})
    assert exc.value.details["field"] == "implementation.entrypoint"


def test_draft_2020_12_marker_is_accepted() -> None:
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    normalized = normalize_contract(
        {
            "inputs": schema,
            "outputs": BASE["outputs"],
            "requiredPermissions": ["tasks.read", "tasks.read"],
            "implementation": {"protocol": "local", "entrypoint": "pkg.mod:fn"},
        }
    )
    assert normalized["inputs"] == schema
    assert normalized["requiredPermissions"] == ["tasks.read"]


@pytest.mark.parametrize("missing", ["inputs", "outputs", "implementation"])
def test_required_fields(missing: str) -> None:
    body = {**BASE, "implementation": {"protocol": "local", "entrypoint": "a:b"}}
    del body[missing]
    with pytest.raises(ValidationError) as exc:
        normalize_contract(body)
    assert exc.value.details["field"] == missing


def test_policy_columns() -> None:
    assert validate_policy_columns("external_write", "high") == ("external_write", "high")
    for side_effects, risk in (("writes", "low"), ("none", "extreme"), (None, "low")):
        with pytest.raises(ValidationError):
            validate_policy_columns(side_effects, risk)


def test_schema_errors_report_paths() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["b"]}
    errors = schema_errors(schema, {"a": "x"})
    assert {e["path"] for e in errors} == {"/", "/a"}
    assert schema_errors(schema, {"a": 1, "b": 0}) == []


def http_contract(auth: object) -> dict[str, object]:
    return {
        **BASE,
        "implementation": {"protocol": "http", "endpoint": "https://svc.test/x", "auth": auth},
    }


def test_auth_scopes_are_a_list_of_scope_strings() -> None:
    auth = {"audience": "svc", "scopes": ["notifications:send", "notifications:send", "a.b"]}
    normalized = normalize_contract(http_contract(auth))
    assert normalized["implementation"]["auth"] == {
        "audience": "svc",
        "scopes": ["notifications:send", "a.b"],
    }
    assert normalize_contract(http_contract({"audience": "svc"}))["implementation"]["auth"] == {
        "audience": "svc"
    }


@pytest.mark.parametrize(
    ("scopes", "code"),
    [
        ("notifications:send", "invalid_skill_contract"),
        ([""], "invalid_skill_contract"),
        ([1], "invalid_skill_contract"),
        (["two scopes"], "invalid_skill_contract"),
        ([f"s{i}" for i in range(21)], "invalid_skill_contract"),
        (["ghp_" + "a" * 36], "secret_material_rejected"),
        (["eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q"], "secret_material_rejected"),
    ],
)
def test_malformed_auth_scopes_are_refused(scopes: object, code: str) -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_contract(http_contract({"audience": "svc", "scopes": scopes}))
    assert exc.value.code == code
    assert exc.value.details["field"] == "implementation.auth.scopes"


def test_replace_endpoint_moves_only_the_address() -> None:
    contract = normalize_contract(
        {
            **BASE,
            "implementation": {
                "protocol": "http",
                "endpoint": "https://old.example/api/v1/skills/notify.send",
                "auth": {"audience": "notification-service", "scopes": ["notifications:send"]},
            },
        }
    )
    moved = replace_endpoint(contract, "https://new.example/api/v1/skills/notify.send")
    assert moved["implementation"]["endpoint"] == "https://new.example/api/v1/skills/notify.send"
    assert {k: v for k, v in moved.items() if k != "implementation"} == {
        k: v for k, v in contract.items() if k != "implementation"
    }
    assert moved["implementation"]["auth"] == contract["implementation"]["auth"]
    assert contract["implementation"]["endpoint"] == "https://old.example/api/v1/skills/notify.send"


@pytest.mark.parametrize("endpoint", ["", None, "ftp://x/y", 42])
def test_replace_endpoint_checks_the_new_value(endpoint: object) -> None:
    contract = normalize_contract(
        {**BASE, "implementation": {"protocol": "http", "endpoint": "https://a.example/x"}}
    )
    with pytest.raises(ValidationError) as exc:
        replace_endpoint(contract, endpoint)
    assert exc.value.details["field"] == "implementation.endpoint"


def test_local_implementation_has_no_endpoint_to_move() -> None:
    contract = normalize_contract(
        {**BASE, "implementation": {"protocol": "local", "entrypoint": "pkg.mod:fn"}}
    )
    with pytest.raises(ValidationError) as exc:
        replace_endpoint(contract, "https://a.example/x")
    assert exc.value.details["protocol"] == "local"
