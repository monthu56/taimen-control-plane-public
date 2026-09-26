"""HRS-2 spike: the manifest compiles deterministically and explains itself.

These tests are the acceptance gate for the design (see
``docs/effective-harness-manifest-plan.md``, step 0): no database, no HTTP —
if reproducibility cannot be proven here, it cannot be claimed anywhere.
"""

from typing import Any

import pytest

from control_plane.domain.canonical import canonical_bytes
from control_plane.domain.errors import ValidationError
from control_plane.domain.harness_manifest import (
    BudgetInput,
    CapturedInput,
    IdentityInput,
    ProjectPolicyInput,
    RunInput,
    ToolInput,
    compile_manifest,
    manifest_hash,
    validate_declared_sections,
    validate_ephemeral,
)

IDENTITY = IdentityInput(
    tenant_id="11111111-1111-1111-1111-111111111111",
    principal_id="22222222-2222-2222-2222-222222222222",
    principal_kind="human",
    session_id="33333333-3333-3333-3333-333333333333",
    control_level="human_operated",
    harness_type="claude-code",
    harness_version="0.6.0",
    protocol_version="2",
    # http is declared but not allowed by governance, local is allowed but not
    # declared: the two visibility dimensions are exercised independently.
    harness_capabilities=("checkpoints", "skills.protocol.mcp", "skills.protocol.http"),
)
RUN = RunInput(
    run_id="44444444-4444-4444-4444-444444444444",
    task_id="55555555-5555-5555-5555-555555555555",
    claim_id="66666666-6666-6666-6666-666666666666",
    attempt=1,
    fencing_token=7,
)
PROJECT = ProjectPolicyInput(
    project_id="77777777-7777-7777-7777-777777777777",
    template_key="delivery-project",
    template_version=1,
    active_revision=3,
    governance={"maxAutonomyLevel": "assisted", "allowedSkillProtocols": ["mcp", "local"]},
    governance_origins={"maxAutonomyLevel": {"source": "revision", "revision": 3}},
    layers=({"projectId": "77777777-7777-7777-7777-777777777777", "depth": 0},),
)
TOOLS = (
    ToolInput(
        skill_id="88888888-8888-8888-8888-888888888888",
        name="deploy",
        version="1.0.0",
        protocol="mcp",
        status="active",
    ),
    ToolInput(
        skill_id="99999999-9999-9999-9999-999999999999",
        name="scrape",
        version="2.1.0",
        protocol="http",
        status="active",
    ),
    ToolInput(
        skill_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        name="build",
        version="1.2.0",
        protocol="local",
        status="active",
    ),
)
BUDGETS = BudgetInput(
    max_duration_seconds=3600,
    max_actions=200,
    governance_ceiling={"maxRunDurationSeconds": 7200, "maxRunActions": 500},
)
CAPTURED = CapturedInput(
    event_cursor="ec1_first",
    task_version=2,
    claim_epoch=1,
    run_attempt=1,
    captured_at="2026-08-12T19:16:04.219744+00:00",
)


def compile_default(**overrides: Any):
    kwargs: dict[str, Any] = {
        "identity": IDENTITY,
        "run": RUN,
        "project_policy": PROJECT,
        "tools": TOOLS,
        "budgets": BUDGETS,
        "captured": CAPTURED,
    }
    kwargs.update(overrides)
    return compile_manifest(**kwargs)


# --- canonical representation -------------------------------------------------


def test_key_order_does_not_change_canonical_bytes() -> None:
    assert canonical_bytes({"b": 1, "a": {"d": 2, "c": 3}}) == canonical_bytes(
        {"a": {"c": 3, "d": 2}, "b": 1}
    )


def test_list_order_is_content() -> None:
    assert canonical_bytes([1, 2]) != canonical_bytes([2, 1])


def test_unicode_is_normalized_to_nfc() -> None:
    composed, decomposed = "é", "é"
    assert composed != decomposed
    assert canonical_bytes({"k": composed}) == canonical_bytes({"k": decomposed})


def test_colliding_keys_after_normalization_are_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        canonical_bytes({"é": 1, "é": 2})
    assert exc.value.code == "non_canonical_value"


def test_float_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        canonical_bytes({"budget": 0.1})
    assert exc.value.code == "non_canonical_value"


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        canonical_bytes({"when": object()})


def test_hash_carries_its_algorithm() -> None:
    digest = manifest_hash({"a": 1})
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_null_is_preserved_as_content() -> None:
    assert canonical_bytes({"a": None}) != canonical_bytes({})


# --- reproducibility (acceptance criterion 1) ---------------------------------


def test_same_inputs_give_byte_stable_document_and_hash() -> None:
    first, second = compile_default(), compile_default()
    assert canonical_bytes(first.base) == canonical_bytes(second.base)
    assert first.base_hash == second.base_hash
    assert first.snapshot_hash == second.snapshot_hash


def test_harness_capability_order_does_not_change_the_hash() -> None:
    shuffled = IdentityInput(
        **{
            **IDENTITY.__dict__,
            "harness_capabilities": tuple(reversed(IDENTITY.harness_capabilities)),
        }
    )
    assert compile_default(identity=shuffled).base_hash == compile_default().base_hash


def test_tool_input_order_does_not_change_the_hash() -> None:
    assert compile_default(tools=tuple(reversed(TOOLS))).base_hash == compile_default().base_hash


# --- versioning trigger (acceptance criterion 2) ------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "project_policy",
            ProjectPolicyInput(**{**PROJECT.__dict__, "active_revision": 4}),
        ),
        (
            "project_policy",
            ProjectPolicyInput(
                **{**PROJECT.__dict__, "governance": {"maxAutonomyLevel": "supervised"}}
            ),
        ),
        (
            "project_policy",
            ProjectPolicyInput(**{**PROJECT.__dict__, "template_version": 2}),
        ),
        ("budgets", BudgetInput(**{**BUDGETS.__dict__, "max_actions": 201})),
        ("tools", TOOLS[:2]),
        (
            "identity",
            IdentityInput(**{**IDENTITY.__dict__, "harness_version": "0.7.0"}),
        ),
        ("run", RunInput(**{**RUN.__dict__, "fencing_token": 8})),
    ],
)
def test_any_effective_revision_change_produces_a_new_hash(field: str, value: Any) -> None:
    assert compile_default(**{field: value}).base_hash != compile_default().base_hash


def test_declared_sections_change_the_hash() -> None:
    with_model = compile_default(declared={"model": {"provider": "anthropic", "model": "opus"}})
    assert with_model.base_hash != compile_default().base_hash


# --- operational vs memory separation (criterion 3) ---------------------------


def test_captured_state_never_enters_the_base_hash() -> None:
    moved = CapturedInput(**{**CAPTURED.__dict__, "event_cursor": "ec1_later", "task_version": 9})
    assert compile_default(captured=moved).base_hash == compile_default().base_hash
    assert compile_default(captured=moved).snapshot_hash != compile_default().snapshot_hash


def test_memory_reference_is_separate_and_outside_the_base_hash() -> None:
    with_memory = compile_default(
        captured=CapturedInput(
            **{
                **CAPTURED.__dict__,
                "memory": {"packId": "ctx-1", "provenance": "memory-service", "lagEvents": 3},
            }
        )
    )
    assert with_memory.base_hash == compile_default().base_hash
    assert with_memory.captured["memory"]["packId"] == "ctx-1"
    assert "memory" not in with_memory.base
    assert set(with_memory.captured) == {"operational", "memory"}


def test_operational_capture_is_present_and_typed() -> None:
    operational = compile_default().captured["operational"]
    assert operational["eventCursor"] == "ec1_first"
    assert operational["claimEpoch"] == 1


# --- provenance and tool visibility -------------------------------------------


def test_every_base_section_has_provenance() -> None:
    compiled = compile_default()
    sections = set(compiled.base) - {"schemaVersion"}
    assert sections <= set(compiled.provenance)
    assert sections == set(compiled.provenance)


def test_tool_visibility_is_explained_per_tool() -> None:
    compiled = compile_default()
    visibility = compiled.provenance["toolPolicy"]["visibility"]
    assert visibility["88888888-8888-8888-8888-888888888888"] == {
        "visible": True,
        "reason": "assigned_and_protocol_supported",
        "source": "principal_skill_assignment",
    }
    # http is executable by the harness but not in allowedSkillProtocols
    assert visibility["99999999-9999-9999-9999-999999999999"] == {
        "visible": False,
        "reason": "protocol_not_allowed_by_governance",
        "source": "principal_skill_assignment",
    }
    # local is allowed by governance but the harness cannot execute it
    assert visibility["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"] == {
        "visible": False,
        "reason": "protocol_not_supported_by_harness",
        "source": "principal_skill_assignment",
    }


def test_tool_policy_reports_both_dimensions_without_contradiction() -> None:
    tools = {t["id"]: t for t in compile_default().base["toolPolicy"]["tools"]}
    http_tool = tools["99999999-9999-9999-9999-999999999999"]
    assert http_tool["executableByHarness"] is True
    assert http_tool["allowedByGovernance"] is False
    assert http_tool["visible"] is False


def test_declared_sections_are_marked_as_declared() -> None:
    compiled = compile_default(
        declared={"executionBackend": {"id": "local-process", "capabilityRevision": 2}}
    )
    assert compiled.provenance["executionBackend"] == {"source": "harness_declared"}
    assert compiled.provenance["workerProfile"] == {"source": "absent"}
    assert compiled.base["workerProfile"] == {"status": "unavailable"}


def test_absent_project_is_reported_as_absent_not_invented() -> None:
    compiled = compile_default(project_policy=ProjectPolicyInput())
    assert compiled.base["projectPolicy"]["projectId"] is None
    assert compiled.provenance["projectPolicy"]["source"] == "absent"


# --- provider fallback (criterion: new recorded attempt) ----------------------


def test_provider_fallback_changes_the_hash_even_for_the_same_provider() -> None:
    first = compile_default(declared={"model": {"provider": "anthropic", "attempt": 1}})
    second = compile_default(declared={"model": {"provider": "anthropic", "attempt": 2}})
    assert first.model_attempt == 1
    assert second.model_attempt == 2
    assert first.base_hash != second.base_hash


def test_model_attempt_defaults_to_one() -> None:
    assert compile_default(declared={"model": {"provider": "anthropic"}}).model_attempt == 1


@pytest.mark.parametrize("attempt", [0, -1, "2", True])
def test_invalid_model_attempt_is_rejected(attempt: Any) -> None:
    with pytest.raises(ValidationError) as exc:
        compile_default(declared={"model": {"attempt": attempt}})
    assert exc.value.code == "invalid_model_attempt"


# --- declaration guards -------------------------------------------------------


@pytest.mark.parametrize("section", ["identity", "run", "projectPolicy", "toolPolicy", "budgets"])
def test_server_authoritative_sections_cannot_be_declared(section: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_declared_sections({section: {"anything": 1}})
    assert exc.value.code == "server_authoritative_section"


def test_unknown_section_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_declared_sections({"promptAssembly": {}})
    assert exc.value.code == "unknown_manifest_section"


def test_secret_material_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_declared_sections({"model": {"apiKey": "sk-live-1"}})
    assert exc.value.code == "secret_material_rejected"


def test_transcript_like_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_declared_sections({"model": {"transcript": ["hello"]}})
    assert exc.value.code == "unsafe_manifest_payload"


def test_absolute_local_path_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_declared_sections({"executionBackend": {"cwd": "/Users/someone/project"}})
    assert exc.value.code == "unsafe_manifest_payload"


def test_oversized_declaration_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_declared_sections({"model": {"notes": "x" * 20_000}})
    assert exc.value.code == "payload_too_large"


def test_float_in_declaration_is_rejected_at_write_time() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_declared_sections({"model": {"temperature": 0.7}})
    assert exc.value.code == "non_canonical_value"


# --- ephemeral markers (criterion 4) ------------------------------------------


def test_ephemeral_kind_is_constrained() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_ephemeral("intent", "s", {})
    assert exc.value.code == "invalid_ephemeral_kind"


def test_ephemeral_summary_is_bounded() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_ephemeral("warning", "x" * 501, {})
    assert exc.value.code == "invalid_ephemeral_summary"


def test_ephemeral_payload_is_guarded_like_the_manifest() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_ephemeral("steering", "focus on tests", {"rawPrompt": "..."})
    assert exc.value.code == "unsafe_manifest_payload"


def test_ephemeral_data_is_canonicalized() -> None:
    assert validate_ephemeral("warning", "budget 80%", {"remaining": 40}) == {"remaining": 40}
