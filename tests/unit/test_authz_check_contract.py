"""``POST /authz:check`` in the contract (CP-ADR-0055, amendment of 2026-09-29).

The actions and resource types the HTTP schema accepts are exactly those the
check has a gate for, and the batch limit is the same on both sides. A gate
turns a 409/422 into a refusal only where the refusal is about what the
resource is; anywhere else it is state after authorization and fails the batch.
"""

import typing
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

from control_plane.api.v1.router import api_v1_router
from control_plane.api.v1.schemas import (
    AUTHZ_CHECK_MAX_ITEMS,
    AuthzAction,
    AuthzResourceType,
)
from control_plane.application.queries import authz_check
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DomainError,
    ValidationError,
)
from control_plane.infrastructure.auth.iam import decision_purpose


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


def test_the_schema_names_every_gated_action_and_nothing_else() -> None:
    assert set(typing.get_args(AuthzAction)) == set(authz_check.ACTION_NAMES)
    assert set(typing.get_args(AuthzResourceType)) == set(authz_check.RESOURCE_TYPES)
    assert AUTHZ_CHECK_MAX_ITEMS == authz_check.CHECK_BATCH_LIMIT
    assert set(authz_check.RESOURCE_TYPES) - {"agent"} == authz_check.UUID_TYPES


def test_the_r011_actions_are_all_answered() -> None:
    assert set(authz_check.ACTIONS) == {
        ("approval", "approve"),
        ("approval", "reject"),
        ("process_instance", "suspend"),
        ("process_instance", "resume"),
        ("process_instance", "cancel"),
        ("run", "request-cancel"),
        ("run", "cancel"),
        ("rule", "enable"),
        ("rule", "disable"),
        ("agent", "update-state"),
        ("principal", "enable"),
        ("principal", "disable"),
    }


def test_the_route_is_in_openapi() -> None:
    spec = _openapi()
    operation = spec["paths"]["/api/v1/authz:check"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["$ref"].endswith("/AuthzCheckRequest")
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/AuthzCheckOut")
    schemas = spec["components"]["schemas"]
    assert schemas["AuthzCheckRequest"]["properties"]["checks"]["maxItems"] == 100
    result = schemas["AuthzCheckResultOut"]["properties"]
    assert {"action", "resourceType", "resourceId", "allowed", "reason"} <= set(result)


def test_only_the_principal_gates_refuse_for_what_the_resource_is() -> None:
    assert {("principal", "enable"), ("principal", "disable")} == authz_check.RESOURCE_REFUSALS


def _refusing(error: DomainError) -> authz_check.Gate:
    async def gate(session: Any, ctx: Any, resource_id: str) -> object:
        raise error

    return gate


STATE_ERRORS = [
    ConflictError("run_not_running", "The run is not running"),
    ValidationError("invalid_state", "Not in this state"),
]


@pytest.mark.parametrize("error", STATE_ERRORS, ids=lambda e: e.code)
async def test_a_state_refusal_of_another_gate_fails_the_batch(
    monkeypatch: pytest.MonkeyPatch, error: DomainError
) -> None:
    # A future gate that checks state after authorization must not read as
    # ``allowed=false``: the check answers only the question of rights.
    monkeypatch.setitem(authz_check.ACTIONS, ("run", "cancel"), _refusing(error))
    item = authz_check.CheckItem("cancel", "run", str(uuid.uuid4()))
    with pytest.raises(type(error)) as raised:
        await authz_check.check(None, None, [item])  # type: ignore[arg-type]
    assert raised.value is error


@pytest.mark.parametrize("error", STATE_ERRORS, ids=lambda e: e.code)
async def test_a_resource_refusal_of_a_principal_gate_is_an_answer(
    monkeypatch: pytest.MonkeyPatch, error: DomainError
) -> None:
    monkeypatch.setitem(authz_check.ACTIONS, ("principal", "enable"), _refusing(error))
    item = authz_check.CheckItem("enable", "principal", str(uuid.uuid4()))
    [result] = await authz_check.check(None, None, [item])  # type: ignore[arg-type]
    assert not result.allowed
    assert result.reason == {"code": error.code, "message": error.message, "details": {}}


def test_a_decision_token_never_reaches_the_check() -> None:
    """``control-plane:decide`` is refused at the door: the check's own
    ``outside_purpose`` branch is a guard, not a path (CP-ADR-0055 A3)."""
    approval = uuid.uuid4()
    claims = {"purpose_ref": f"approval:{approval}"}
    with pytest.raises(AuthorizationError) as refused:
        decision_purpose(claims, "POST /api/v1/authz:check")
    assert refused.value.code == "outside_purpose"
    assert decision_purpose(claims, f"POST /api/v1/approvals/{approval}:approve") == (
        f"approval:{approval}"
    )
