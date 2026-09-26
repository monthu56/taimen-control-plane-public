"""Skill contract v1 (ADR-0056 §1): validation at publication time.

A contract is checked once, when the version is published, and stored in its
normalized form — every optional field filled with its default — so that the
invocation path reads a complete document and never re-derives defaults. The
version is immutable afterwards, so "valid at publication" stays true.

Pure functions only: no I/O, no database.
"""

import re
from typing import Any

import jsonschema

from control_plane.domain.enums import (
    ALL_PERMISSIONS,
    INVOCABLE_SKILL_PROTOCOLS,
    SkillIdempotency,
    SkillProtocol,
    SkillRiskLevel,
    SkillSideEffects,
)
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import (
    guard_json_document,
    reject_secret_material,
    validate_json_schema_document,
)
from control_plane.domain.redaction import secret_material

CONTRACT_FIELDS = frozenset(
    {
        "inputs",
        "outputs",
        "requiredPermissions",
        "preconditions",
        "postconditions",
        "timeoutSeconds",
        "retryPolicy",
        "idempotency",
        "costModel",
        "implementation",
    }
)
IMPLEMENTATION_FIELDS = frozenset({"protocol", "endpoint", "auth", "entrypoint"})

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600
MAX_ATTEMPTS_LIMIT = 10
MAX_BACKOFF_SECONDS = 3600
MAX_AUTH_SCOPES = 20

JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# ``package.module:function`` — what a local executor imports (ADR-0056 §5).
_ENTRYPOINT_RE = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*:[A-Za-z_][\w]*$")
_MCP_TOOL_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,199}$")
# One OAuth scope token (RFC 6749 §3.3 without the exotic punctuation), e.g.
# ``notifications:send``: a space would split it into two on the wire.
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:*/-]{1,200}$")


def _invalid(message: str, *, field: str, **details: Any) -> ValidationError:
    return ValidationError("invalid_skill_contract", message, details={"field": field, **details})


def _schema(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{field} must be a JSON Schema object", field=field)
    declared = value.get("$schema")
    if declared is not None and declared.rstrip("#") != JSON_SCHEMA_2020_12:
        raise _invalid(
            f"{field} must use JSON Schema draft 2020-12",
            field=field,
            schema=str(declared)[:200],
        )
    validate_json_schema_document(value, field_name=field)
    return value


def _positive_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _invalid(
            f"{field} must be an integer between {minimum} and {maximum}",
            field=field,
        )
    return value


def _conditions(value: Any, *, field: str) -> list[Any]:
    """Pre/postconditions — accepted only empty until the rule language (M1.3).

    Storing an expression the core cannot evaluate would publish a contract
    that silently promises a check nobody makes; better to refuse it now.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise _invalid(f"{field} must be a list", field=field)
    if value:
        raise ValidationError(
            "unsupported_skill_condition",
            f"{field} are not supported yet: the expression language arrives with M1.3",
            details={"field": field},
        )
    return []


def _auth_scopes(value: Any) -> list[str]:
    """``implementation.auth.scopes`` — what the executor asks IAM for.

    A request, not a grant: IAM cuts it by the executor PAT's scope ceiling.
    The list is published with the contract, so it must not carry anything
    shaped like a credential.
    """
    field = "implementation.auth.scopes"
    if not isinstance(value, list) or len(value) > MAX_AUTH_SCOPES:
        raise _invalid(
            f"{field} must be a list of at most {MAX_AUTH_SCOPES} scopes",
            field=field,
            maxItems=MAX_AUTH_SCOPES,
        )
    for index, item in enumerate(value):
        if not isinstance(item, str) or _SCOPE_RE.match(item) is None:
            raise _invalid(
                f"{field} must hold non-empty scope strings without spaces",
                field=field,
                index=index,
            )
        if secret_material(item) is not None:
            raise ValidationError(
                "secret_material_rejected",
                "Secrets must not be stored here; use an opaque secretRef instead",
                details={"field": field, "index": index},
            )
    return list(dict.fromkeys(value))


def _implementation(value: Any) -> dict[str, Any]:
    field = "implementation"
    if not isinstance(value, dict):
        raise _invalid("implementation must be an object", field=field)
    unknown = sorted(set(value) - IMPLEMENTATION_FIELDS)
    if unknown:
        raise _invalid("Unknown implementation fields", field=field, unknown=unknown)
    protocol = value.get("protocol")
    if protocol not in INVOCABLE_SKILL_PROTOCOLS:
        raise _invalid(
            "implementation.protocol must be one of http, local, mcp",
            field="implementation.protocol",
            allowed=sorted(INVOCABLE_SKILL_PROTOCOLS),
        )
    endpoint = value.get("endpoint")
    entrypoint = value.get("entrypoint")
    auth = value.get("auth")
    for name, item in (("endpoint", endpoint), ("entrypoint", entrypoint)):
        if item is not None and (not isinstance(item, str) or not item or len(item) > 2000):
            raise _invalid(f"implementation.{name} must be a non-empty string", field=name)
    if auth is not None:
        if not isinstance(auth, dict):
            raise _invalid("implementation.auth must be an object", field="implementation.auth")
        # The contract is readable by every caller: a credential here would be
        # a published secret. Name the audience or a secretRef instead.
        reject_secret_material(auth, label="implementation.auth")
        if "scopes" in auth:
            auth = {**auth, "scopes": _auth_scopes(auth["scopes"])}

    if protocol == SkillProtocol.HTTP:
        if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
            raise _invalid(
                "http implementation needs an http(s) endpoint", field="implementation.endpoint"
            )
    elif protocol == SkillProtocol.LOCAL:
        if not isinstance(entrypoint, str) or _ENTRYPOINT_RE.match(entrypoint) is None:
            raise _invalid(
                "local implementation needs entrypoint 'module:function'",
                field="implementation.entrypoint",
            )
    elif protocol == SkillProtocol.MCP and (
        not isinstance(entrypoint, str) or _MCP_TOOL_RE.match(entrypoint) is None
    ):
        raise _invalid(
            "mcp implementation needs entrypoint = the MCP tool name",
            field="implementation.entrypoint",
        )
    return {
        "protocol": str(protocol),
        "endpoint": endpoint,
        "auth": auth,
        "entrypoint": entrypoint,
    }


def normalize_contract(contract: Any) -> dict[str, Any]:
    """Validate a contract v1 document and return it with defaults filled in."""
    if not isinstance(contract, dict):
        raise _invalid("contract must be an object", field="contract")
    guard_json_document(contract, label="contract")
    unknown = sorted(set(contract) - CONTRACT_FIELDS)
    if unknown:
        raise _invalid("Unknown contract fields", field="contract", unknown=unknown)
    for required in ("inputs", "outputs", "implementation"):
        if required not in contract:
            raise _invalid(f"contract.{required} is required", field=required)

    inputs = _schema(contract["inputs"], field="inputs")
    outputs = _schema(contract["outputs"], field="outputs")

    permissions = contract.get("requiredPermissions") or []
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        raise _invalid("requiredPermissions must be a list of strings", field="requiredPermissions")
    unknown_permissions = sorted(set(permissions) - ALL_PERMISSIONS)
    if unknown_permissions:
        raise _invalid(
            "requiredPermissions names unknown permissions",
            field="requiredPermissions",
            unknown=unknown_permissions,
        )

    timeout = _positive_int(
        contract.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS),
        field="timeoutSeconds",
        minimum=1,
        maximum=MAX_TIMEOUT_SECONDS,
    )

    retry = contract.get("retryPolicy") or {}
    if not isinstance(retry, dict) or set(retry) - {"maxAttempts", "backoffSeconds"}:
        raise _invalid("retryPolicy must be {maxAttempts, backoffSeconds}", field="retryPolicy")
    max_attempts = _positive_int(
        retry.get("maxAttempts", 1),
        field="retryPolicy.maxAttempts",
        minimum=1,
        maximum=MAX_ATTEMPTS_LIMIT,
    )
    backoff = _positive_int(
        retry.get("backoffSeconds", 0),
        field="retryPolicy.backoffSeconds",
        minimum=0,
        maximum=MAX_BACKOFF_SECONDS,
    )

    idempotency = contract.get("idempotency", SkillIdempotency.NONE)
    if idempotency not in set(SkillIdempotency):
        raise _invalid(
            "idempotency must be required, natural or none",
            field="idempotency",
            allowed=sorted(SkillIdempotency),
        )

    cost_model = contract.get("costModel")
    if cost_model is not None and (
        not isinstance(cost_model, dict)
        or set(cost_model) - {"unit", "estimate"}
        or not isinstance(cost_model.get("unit"), str)
        or not cost_model["unit"]
        or isinstance(cost_model.get("estimate"), bool)
        or not isinstance(cost_model.get("estimate", 0), int | float)
        or cost_model.get("estimate", 0) < 0
    ):
        raise _invalid("costModel must be {unit: string, estimate: number >= 0}", field="costModel")

    return {
        "inputs": inputs,
        "outputs": outputs,
        "requiredPermissions": sorted(set(permissions)),
        "preconditions": _conditions(contract.get("preconditions"), field="preconditions"),
        "postconditions": _conditions(contract.get("postconditions"), field="postconditions"),
        "timeoutSeconds": timeout,
        "retryPolicy": {"maxAttempts": max_attempts, "backoffSeconds": backoff},
        "idempotency": str(idempotency),
        "costModel": cost_model,
        "implementation": _implementation(contract["implementation"]),
    }


def validate_policy_columns(side_effects: Any, risk_level: Any) -> tuple[str, str]:
    """``sideEffects``/``riskLevel`` are mandatory next to a contract."""
    if side_effects not in set(SkillSideEffects):
        raise _invalid(
            "sideEffects must be none, external_read or external_write",
            field="sideEffects",
            allowed=sorted(SkillSideEffects),
        )
    if risk_level not in set(SkillRiskLevel):
        raise _invalid(
            "riskLevel must be low, medium or high",
            field="riskLevel",
            allowed=sorted(SkillRiskLevel),
        )
    return str(side_effects), str(risk_level)


def require_safe_retries(contract: dict[str, Any], side_effects: str) -> None:
    """An external write without idempotency is attempted at most once.

    A retry — after a retryable failure or an expired lease — repeats the
    call, and the executor may have acted the first time. For
    ``external_write`` that is a second external action unless the target
    deduplicates: so either ``idempotency`` is ``required``/``natural`` or
    ``retryPolicy.maxAttempts`` is 1 (review of M2.1).
    """
    if (
        side_effects == SkillSideEffects.EXTERNAL_WRITE
        and contract["idempotency"] == SkillIdempotency.NONE
        and contract["retryPolicy"]["maxAttempts"] > 1
    ):
        raise _invalid(
            "an external_write skill without idempotency may not be retried: "
            "set idempotency to required or natural, or retryPolicy.maxAttempts to 1",
            field="retryPolicy.maxAttempts",
            maxAttempts=contract["retryPolicy"]["maxAttempts"],
            idempotency=contract["idempotency"],
        )


def schema_errors(schema: dict[str, Any], instance: Any) -> list[dict[str, str]]:
    """Every violation of ``schema`` by ``instance`` with a stable JSON path.

    ``format`` is asserted, not merely annotated: a contract saying
    ``"format": "uuid"`` means the executor may rely on it. Formats the
    installed checker does not know stay annotations (ADR-0056 amendment).
    """
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    try:
        found = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    except Exception as exc:  # an unresolvable $ref must not become a 500
        return [{"path": "/", "message": f"schema could not be evaluated: {exc}"[:300]}]
    return [
        {
            "path": "/" + "/".join(str(part) for part in error.absolute_path),
            "message": error.message[:500],
        }
        for error in found
    ][:20]
