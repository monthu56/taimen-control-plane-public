"""HRS-7 spike: narrowing is monotone and a child result is provable.

These tests are the acceptance gate for the design (see
``docs/plans/TASK-000007-child-run-handle.md``, step 0): no database, no HTTP.
If the ceiling can widen or a result cannot be hashed reproducibly here,
neither claim can be made anywhere else.
"""

import uuid

import pytest

from control_plane.domain.child_handle import (
    MAX_ARTIFACT_REFS,
    MAX_CHILD_DEPTH,
    MAX_SUMMARY_CHARS,
    Grant,
    GrantRequest,
    build_result_document,
    child_depth,
    expand_permissions,
    grant_covers,
    grant_from_stored,
    hash_token_secret,
    issue_token,
    looks_like_token,
    narrow_grant,
    normalize_grant,
    parse_token,
    result_hash,
    root_ceiling,
    token_secret_matches,
    validate_cancellation_policy,
    validate_correlation_id,
    validate_expiry_seconds,
)
from control_plane.domain.enums import ALL_PERMISSIONS, Permission
from control_plane.domain.errors import ValidationError

PARENT = Grant(
    permissions=("artifacts.write", "tasks.claim", "tasks.read", "tasks.write"),
    capabilities=("deploy",),
    skills=("migration-check@2", "review@1"),
)


# --- narrowing ----------------------------------------------------------------


def test_requested_subset_becomes_the_child_ceiling() -> None:
    granted = narrow_grant(PARENT, GrantRequest(permissions=("tasks.read",), skills=("review@1",)))
    assert granted.permissions == ("tasks.read",)
    assert granted.skills == ("review@1",)
    # capabilities were not mentioned -> inherited
    assert granted.capabilities == PARENT.capabilities


def test_unset_request_inherits_the_whole_parent_ceiling() -> None:
    assert narrow_grant(PARENT, None) == PARENT
    assert narrow_grant(PARENT, GrantRequest()) == PARENT


def test_explicitly_empty_list_grants_nothing_and_is_not_inheritance() -> None:
    granted = narrow_grant(PARENT, GrantRequest(permissions=()))
    assert granted.permissions == ()
    assert granted.capabilities == PARENT.capabilities


def test_excess_is_rejected_rather_than_trimmed() -> None:
    with pytest.raises(ValidationError) as excinfo:
        narrow_grant(PARENT, GrantRequest(permissions=("tasks.read", "claims.manage")))
    assert excinfo.value.code == "child_grant_exceeds_parent"
    assert excinfo.value.details["excess"]["permissions"] == ["claims.manage"]


def test_excess_reports_every_dimension_at_once() -> None:
    with pytest.raises(ValidationError) as excinfo:
        narrow_grant(
            PARENT,
            GrantRequest(
                permissions=("org.manage",), capabilities=("billing",), skills=("secret-skill@1",)
            ),
        )
    excess = excinfo.value.details["excess"]
    assert set(excess) == {"permissions", "capabilities", "skills"}


def test_ceiling_is_monotone_across_three_levels() -> None:
    level1 = narrow_grant(PARENT, GrantRequest(permissions=("tasks.read", "tasks.claim")))
    level2 = narrow_grant(level1, GrantRequest(permissions=("tasks.read",)))
    with pytest.raises(ValidationError):
        # the grandchild may not recover what its parent gave up
        narrow_grant(level2, GrantRequest(permissions=("tasks.claim",)))
    level3 = narrow_grant(level2, None)
    assert level3.permissions == ("tasks.read",)


def test_admin_parent_covers_any_named_permission_but_child_is_literal() -> None:
    admin_parent = root_ceiling(permissions=frozenset({Permission.ADMIN.value}))
    granted = narrow_grant(admin_parent, GrantRequest(permissions=("tasks.read",)))
    assert granted.permissions == ("tasks.read",)
    assert not grant_covers(granted, Permission.CLAIMS_MANAGE)
    assert grant_covers(admin_parent, Permission.CLAIMS_MANAGE)


def test_admin_expands_to_every_permission() -> None:
    assert expand_permissions(("admin",)) == frozenset(ALL_PERMISSIONS)
    assert expand_permissions(("tasks.read",)) == frozenset({"tasks.read"})


def test_normalize_grant_sorts_deduplicates_and_rejects_unknown_permissions() -> None:
    request = normalize_grant({"permissions": ["tasks.read", "tasks.read", "artifacts.write"]})
    assert request.permissions == ("artifacts.write", "tasks.read")
    assert request.capabilities is None
    with pytest.raises(ValidationError) as excinfo:
        normalize_grant({"permissions": ["tasks.teleport"]})
    assert excinfo.value.details["unknown"] == ["tasks.teleport"]


def test_normalize_grant_rejects_unknown_fields_and_wrong_shapes() -> None:
    with pytest.raises(ValidationError):
        normalize_grant({"roles": ["admin"]})
    with pytest.raises(ValidationError):
        normalize_grant({"permissions": "tasks.read"})
    with pytest.raises(ValidationError):
        normalize_grant({"skills": [""]})


def test_stored_grant_survives_a_permission_leaving_the_enum() -> None:
    stored = grant_from_stored({"permissions": ["tasks.read", "legacy.permission"]})
    assert stored.permissions == ("legacy.permission", "tasks.read")
    assert grant_from_stored(None) == Grant()


# --- correlation, depth, expiry, policy ---------------------------------------


def test_correlation_id_is_bounded_and_trimmed() -> None:
    assert validate_correlation_id("  review:migration-1 ") == "review:migration-1"
    for bad in ("", "  ", "a" * 129, "spaces inside", "emoji-🙂"):
        with pytest.raises(ValidationError) as excinfo:
            validate_correlation_id(bad)
        assert excinfo.value.code == "invalid_correlation_id"


def test_depth_is_capped() -> None:
    assert child_depth(0) == 1
    with pytest.raises(ValidationError) as excinfo:
        child_depth(MAX_CHILD_DEPTH)
    assert excinfo.value.code == "child_depth_exceeded"


def test_expiry_and_policy_defaults_are_explicit() -> None:
    assert validate_expiry_seconds(None) > 0
    assert validate_expiry_seconds(60) == 60
    for bad in (0, -1, True, 10**9):
        with pytest.raises(ValidationError):
            validate_expiry_seconds(bad)  # type: ignore[arg-type]
    assert validate_cancellation_policy(None) == "cascade_cooperative"
    assert validate_cancellation_policy("detach") == "detach"
    with pytest.raises(ValidationError):
        validate_cancellation_policy("ignore_everything")


# --- token --------------------------------------------------------------------


def test_token_round_trip_and_digest_only_storage() -> None:
    handle_id = uuid.uuid4()
    issued = issue_token(handle_id)
    assert looks_like_token(issued.token)
    parsed_id, secret = parse_token(issued.token)
    assert parsed_id == handle_id
    assert secret not in issued.secret_hash
    assert token_secret_matches(secret, issued.secret_hash)
    assert not token_secret_matches(secret + "x", issued.secret_hash)
    assert issued.secret_hash == hash_token_secret(secret)


def test_two_tokens_for_the_same_handle_never_collide() -> None:
    handle_id = uuid.uuid4()
    assert issue_token(handle_id).token != issue_token(handle_id).token


def test_malformed_tokens_are_rejected_before_any_lookup() -> None:
    for bad in ("", "ch1_", "ch2_abc_def", "ch1_not-a-uuid_secret", "ch1_" + "a" * 32):
        with pytest.raises(ValidationError) as excinfo:
            parse_token(bad)
        assert excinfo.value.code == "invalid_child_handle_token"
    assert not looks_like_token("cp_abc_def")


def test_token_secret_matches_is_false_for_empty_stored_hash() -> None:
    assert not token_secret_matches("whatever", "")


# --- bounded terminal result ---------------------------------------------------


def test_result_hash_is_stable_and_independent_of_key_order() -> None:
    first = build_result_document(
        outcome="succeeded",
        summary="migration roundtrip verified",
        data={"checked": 3, "nested": {"b": 2, "a": 1}},
        artifact_refs=[str(uuid.UUID(int=2)), str(uuid.UUID(int=1))],
    )
    second = build_result_document(
        outcome="succeeded",
        summary="migration roundtrip verified",
        data={"nested": {"a": 1, "b": 2}, "checked": 3},
        artifact_refs=[uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=1)],
    )
    assert first == second
    assert result_hash(first) == result_hash(second)
    assert result_hash(first).startswith("sha256:")


def test_result_hash_changes_with_every_meaningful_field() -> None:
    base = build_result_document(outcome="succeeded", summary="done", data={"n": 1})
    mutations = (
        build_result_document(outcome="failed", summary="done", data={"n": 1}),
        build_result_document(outcome="succeeded", summary="done differently", data={"n": 1}),
        build_result_document(outcome="succeeded", summary="done", data={"n": 2}),
        build_result_document(
            outcome="succeeded", summary="done", data={"n": 1}, artifact_refs=[uuid.UUID(int=7)]
        ),
    )
    hashes = {result_hash(document) for document in mutations}
    assert result_hash(base) not in hashes
    assert len(hashes) == len(mutations)


def test_result_document_survives_a_json_round_trip_byte_for_byte() -> None:
    import json

    document = build_result_document(
        outcome="succeeded", summary="ünïcode ok", data={"k": [1, 2, {"deep": None}]}
    )
    restored = json.loads(json.dumps(document))
    assert result_hash(restored) == result_hash(document)


def test_oversized_result_is_rejected_not_truncated() -> None:
    with pytest.raises(ValidationError) as excinfo:
        build_result_document(outcome="succeeded", summary="x" * (MAX_SUMMARY_CHARS + 1))
    assert excinfo.value.code == "child_result_too_large"

    with pytest.raises(ValidationError) as excinfo:
        build_result_document(
            outcome="succeeded",
            summary="ok",
            artifact_refs=[uuid.UUID(int=index) for index in range(MAX_ARTIFACT_REFS + 1)],
        )
    assert excinfo.value.code == "child_result_too_large"

    with pytest.raises(ValidationError) as excinfo:
        build_result_document(outcome="succeeded", summary="ok", data={"blob": "y" * (32 * 1024)})
    assert excinfo.value.code == "payload_too_large"


def test_result_refuses_transcripts_secrets_and_local_paths() -> None:
    for payload in (
        {"transcript": ["hello"]},
        {"api_key": "value"},
        {"where": "/Users/someone/project/out.txt"},
    ):
        with pytest.raises(ValidationError):
            build_result_document(outcome="succeeded", summary="ok", data=payload)
    with pytest.raises(ValidationError):
        build_result_document(outcome="succeeded", summary="see /home/me/log.txt")


def test_result_rejects_unknown_outcome_empty_summary_and_bad_refs() -> None:
    with pytest.raises(ValidationError):
        build_result_document(outcome="partially", summary="ok")
    with pytest.raises(ValidationError):
        build_result_document(outcome="succeeded", summary="   ")
    with pytest.raises(ValidationError):
        build_result_document(outcome="succeeded", summary="ok", artifact_refs=["not-a-uuid"])
    with pytest.raises(ValidationError):
        build_result_document(outcome="succeeded", summary="ok", data=[1, 2])  # type: ignore[arg-type]


def test_result_rejects_values_that_cannot_be_canonicalized() -> None:
    with pytest.raises(ValidationError) as excinfo:
        build_result_document(outcome="succeeded", summary="ok", data={"ratio": 0.5})
    assert excinfo.value.code == "non_canonical_value"
