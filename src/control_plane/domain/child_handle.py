"""Durable Child Run Handle: pure functions (HRS-7).

No database, no HTTP, no clock — the same reason as ``harness_manifest``: the
two claims this module makes are checkable by a table of unit cases.

``narrow_grant``
    The ceiling of a child is the intersection of what was requested with what
    the parent itself may do. Because a child's own children are narrowed
    against *its* ceiling rather than against its API key, the ceiling can only
    shrink down the tree.
``result_hash``
    The bounded terminal result of a child is hashed over the shared canonical
    form (``domain/canonical.py``), so "the parent read exactly this result" is
    provable without storing a transcript.

The handle token is a versioned opaque *locator*, never a credential: the
secret only makes a handle id unguessable, and every access still performs a
server-side lookup plus tenant and permission checks.

See ``docs/specs/TASK-000007-child-run-handle.md``.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from control_plane.domain.canonical import canonical_bytes, content_hash
from control_plane.domain.enums import ALL_PERMISSIONS, Permission
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import guard_json_document, reject_secret_material
from control_plane.domain.redaction import reject_unsafe_durable_payload

SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256"

#: Token format version. It travels inside the token so a future format is a
#: visible change rather than a silent reinterpretation of old strings.
TOKEN_VERSION = 1
TOKEN_PREFIX = "ch1"
_TOKEN_SECRET_BYTES = 32

CANCELLATION_POLICIES = ("cascade_cooperative", "detach")
DEFAULT_CANCELLATION_POLICY = "cascade_cooperative"

CHILD_OUTCOMES = ("succeeded", "failed", "cancelled")

#: Derived, never stored: see the SPEC on why the handle keeps no status.
DERIVED_STATUSES = (
    "pending",
    "running",
    "suspended",
    "succeeded",
    "failed",
    "cancelled",
    "revoked",
    "expired",
)

MAX_CORRELATION_CHARS = 128
MAX_SUMMARY_CHARS = 2_000
MAX_ARTIFACT_REFS = 50
MAX_RESULT_DATA_BYTES = 16 * 1024
MAX_GRANT_ENTRIES = 100
MAX_GRANT_ENTRY_CHARS = 200
MAX_CHILD_DEPTH = 8

#: A child handle lives no longer than this unless the caller asks for less.
DEFAULT_EXPIRY_SECONDS = 7 * 24 * 3600
MAX_EXPIRY_SECONDS = 90 * 24 * 3600

CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_UNSAFE_CODE = "unsafe_child_result"
_UNSAFE_SUBJECT = "Child result"


# --- grants -------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    """What a child run may do, as three sorted, deduplicated string sets.

    Permissions are API permissions; capabilities and skills are opaque refs
    resolved elsewhere. The dataclass carries no authority by itself — it is
    the *record* of a ceiling that the command layer enforces.
    """

    permissions: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "permissions": list(self.permissions),
            "capabilities": list(self.capabilities),
            "skills": list(self.skills),
        }

    def is_empty(self) -> bool:
        return not (self.permissions or self.capabilities or self.skills)


def _normalize_entries(values: Any, *, field: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise ValidationError("invalid_child_grant", f"grant.{field} must be a list of strings")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                "invalid_child_grant", f"grant.{field} entries must be non-empty strings"
            )
        entry = value.strip()
        if len(entry) > MAX_GRANT_ENTRY_CHARS:
            raise ValidationError(
                "invalid_child_grant",
                f"grant.{field} entry exceeds the {MAX_GRANT_ENTRY_CHARS}-character limit",
            )
        normalized.add(entry)
    if len(normalized) > MAX_GRANT_ENTRIES:
        raise ValidationError(
            "invalid_child_grant",
            f"grant.{field} exceeds the {MAX_GRANT_ENTRIES}-entry limit",
            details={"maxEntries": MAX_GRANT_ENTRIES},
        )
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class GrantRequest:
    """What a launcher asked for. ``None`` means "inherit that field".

    The distinction between ``None`` and ``()`` is load-bearing: an omitted
    field inherits the parent ceiling, an explicitly empty list grants nothing.
    Collapsing the two would turn a forgotten field into a child that fails
    every authoritative write with no visible cause.
    """

    permissions: tuple[str, ...] | None = None
    capabilities: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None


def normalize_grant(value: Any) -> GrantRequest:
    """Accept a request-shaped mapping and return a canonical ``GrantRequest``."""
    if value is None:
        return GrantRequest()
    if not isinstance(value, dict):
        raise ValidationError("invalid_child_grant", "grant must be an object")
    unknown = set(value) - {"permissions", "capabilities", "skills"}
    if unknown:
        raise ValidationError(
            "invalid_child_grant",
            "grant accepts only permissions, capabilities and skills",
            details={"unknown": sorted(unknown)},
        )

    def field(name: str) -> tuple[str, ...] | None:
        raw = value.get(name)
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise ValidationError("invalid_child_grant", f"grant.{name} must be a list of strings")
        return _normalize_entries(raw, field=name)

    permissions = field("permissions")
    if permissions is not None:
        unknown_permissions = sorted(set(permissions) - ALL_PERMISSIONS)
        if unknown_permissions:
            raise ValidationError(
                "invalid_child_grant",
                "grant.permissions contains unknown permissions",
                details={"unknown": unknown_permissions},
            )
    return GrantRequest(
        permissions=permissions,
        capabilities=field("capabilities"),
        skills=field("skills"),
    )


def expand_permissions(permissions: tuple[str, ...] | frozenset[str]) -> frozenset[str]:
    """``admin`` stands for every permission; everything else is literal."""
    values = frozenset(permissions)
    if Permission.ADMIN.value in values:
        return frozenset(ALL_PERMISSIONS)
    return values


def root_ceiling(
    *,
    permissions: frozenset[str],
    capabilities: tuple[str, ...] = (),
    skills: tuple[str, ...] = (),
) -> Grant:
    """The implicit ceiling of a run that was not itself launched by a handle.

    Permissions are stored verbatim (``admin`` stays ``admin``) so the record
    shows what was actually inherited rather than a snapshot of the permission
    list as it happened to look that day.
    """
    return Grant(
        permissions=tuple(sorted(permissions)),
        capabilities=tuple(sorted(set(capabilities))),
        skills=tuple(sorted(set(skills))),
    )


def narrow_grant(parent: Grant, requested: GrantRequest | None) -> Grant:
    """Return the child ceiling, or raise if the request exceeds the parent.

    Excess is rejected rather than silently intersected away: asking for more
    than the parent holds is an orchestrator bug, and quietly trimming it would
    let that bug resurface later as a mysteriously powerless child.
    """
    request = requested if requested is not None else GrantRequest()
    excess: dict[str, list[str]] = {}

    if request.permissions is None:
        permissions = parent.permissions
    else:
        over = sorted(
            expand_permissions(request.permissions) - expand_permissions(parent.permissions)
        )
        if over:
            excess["permissions"] = over
        permissions = request.permissions

    if request.capabilities is None:
        capabilities = parent.capabilities
    else:
        over = sorted(frozenset(request.capabilities) - frozenset(parent.capabilities))
        if over:
            excess["capabilities"] = over
        capabilities = request.capabilities

    if request.skills is None:
        skills = parent.skills
    else:
        over = sorted(frozenset(request.skills) - frozenset(parent.skills))
        if over:
            excess["skills"] = over
        skills = request.skills

    if excess:
        raise ValidationError(
            "child_grant_exceeds_parent",
            "Requested child grant exceeds the parent ceiling",
            details={"excess": excess},
        )
    return Grant(permissions=permissions, capabilities=capabilities, skills=skills)


def grant_covers(granted: Grant, permission: Permission) -> bool:
    """Is one permission inside a stored ceiling? (``admin`` covers all.)"""
    return permission.value in expand_permissions(granted.permissions)


def grant_from_stored(value: Any) -> Grant:
    """Rebuild a ``Grant`` from the JSONB column without re-validating names.

    Stored rows were validated on the way in; a permission removed from the
    enum later must still describe the ceiling that was actually in force.
    """
    if not isinstance(value, dict):
        return Grant()

    def entries(field: str) -> tuple[str, ...]:
        raw = value.get(field)
        if not isinstance(raw, list):
            return ()
        return tuple(sorted({item for item in raw if isinstance(item, str) and item}))

    return Grant(
        permissions=entries("permissions"),
        capabilities=entries("capabilities"),
        skills=entries("skills"),
    )


# --- correlation, depth, expiry ------------------------------------------------


def validate_correlation_id(value: str) -> str:
    """The domain idempotency key of a launch: bounded and caller-chosen."""
    if not isinstance(value, str) or not CORRELATION_ID_PATTERN.match(value.strip()):
        raise ValidationError(
            "invalid_correlation_id",
            "correlationId must match ^[A-Za-z0-9._:-]{1,128}$",
            details={"maxChars": MAX_CORRELATION_CHARS},
        )
    return value.strip()


def child_depth(parent_depth: int) -> int:
    """Depth of the next level; raises once the tree is too deep."""
    depth = parent_depth + 1
    if depth > MAX_CHILD_DEPTH:
        raise ValidationError(
            "child_depth_exceeded",
            f"Child run nesting exceeds the {MAX_CHILD_DEPTH}-level limit",
            details={"maxDepth": MAX_CHILD_DEPTH},
        )
    return depth


def validate_expiry_seconds(value: int | None) -> int:
    if value is None:
        return DEFAULT_EXPIRY_SECONDS
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError("invalid_child_expiry", "expiresInSeconds must be a positive integer")
    if value > MAX_EXPIRY_SECONDS:
        raise ValidationError(
            "invalid_child_expiry",
            f"expiresInSeconds exceeds the {MAX_EXPIRY_SECONDS}-second limit",
            details={"maxSeconds": MAX_EXPIRY_SECONDS},
        )
    return value


def validate_cancellation_policy(value: str | None) -> str:
    if value is None:
        return DEFAULT_CANCELLATION_POLICY
    if value not in CANCELLATION_POLICIES:
        raise ValidationError(
            "invalid_cancellation_policy",
            f"Unknown cancellation policy: {value}",
            details={"allowed": list(CANCELLATION_POLICIES)},
        )
    return value


# --- token --------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class IssuedToken:
    token: str
    secret_hash: str


def issue_token(handle_id: uuid.UUID) -> IssuedToken:
    """Mint ``ch1_<hex id>_<secret>``; only the secret digest is stored."""
    secret = _b64(secrets.token_bytes(_TOKEN_SECRET_BYTES))
    return IssuedToken(
        token=f"{TOKEN_PREFIX}_{handle_id.hex}_{secret}",
        secret_hash=hash_token_secret(secret),
    )


def hash_token_secret(secret: str) -> str:
    return f"{HASH_ALGORITHM}:{hashlib.sha256(secret.encode()).hexdigest()}"


def parse_token(token: str) -> tuple[uuid.UUID, str]:
    """Split a handle token, or raise. Never decides authorization by itself."""
    if not isinstance(token, str):
        raise ValidationError("invalid_child_handle_token", "Handle token must be a string")
    parts = token.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[2]:
        raise ValidationError(
            "invalid_child_handle_token",
            "Handle token must look like 'ch1_<id>_<secret>'",
        )
    try:
        handle_id = uuid.UUID(hex=parts[1])
    except (ValueError, binascii.Error) as exc:
        raise ValidationError(
            "invalid_child_handle_token", "Handle token carries a malformed id"
        ) from exc
    return handle_id, parts[2]


def token_secret_matches(secret: str, stored_hash: str) -> bool:
    """Constant-time comparison: a wrong secret must not be measurably faster."""
    return hmac.compare_digest(hash_token_secret(secret), stored_hash or "")


def looks_like_token(value: str) -> bool:
    return isinstance(value, str) and value.strip().startswith(f"{TOKEN_PREFIX}_")


# --- bounded terminal result ---------------------------------------------------


def _validate_summary(summary: str) -> str:
    if not isinstance(summary, str) or not summary.strip():
        raise ValidationError("invalid_child_result", "Child result summary must not be empty")
    value = summary.strip()
    if len(value) > MAX_SUMMARY_CHARS:
        raise ValidationError(
            "child_result_too_large",
            f"Child result summary exceeds the {MAX_SUMMARY_CHARS}-character limit",
            details={"field": "summary", "maxChars": MAX_SUMMARY_CHARS},
        )
    reject_unsafe_durable_payload(value, code=_UNSAFE_CODE, subject=_UNSAFE_SUBJECT)
    return value


def _validate_artifact_refs(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValidationError("invalid_child_result", "artifactRefs must be a list")
    refs: list[str] = []
    for value in values:
        try:
            refs.append(str(uuid.UUID(str(value))))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValidationError(
                "invalid_child_result", "artifactRefs entries must be artifact ids"
            ) from exc
    unique = sorted(set(refs))
    if len(unique) > MAX_ARTIFACT_REFS:
        raise ValidationError(
            "child_result_too_large",
            f"Child result carries more than {MAX_ARTIFACT_REFS} artifact references",
            details={"field": "artifactRefs", "maxRefs": MAX_ARTIFACT_REFS},
        )
    return unique


def build_result_document(
    *,
    outcome: str,
    summary: str,
    data: dict[str, Any] | None = None,
    artifact_refs: Any = None,
) -> dict[str, Any]:
    """Bounded, canonically representable terminal result of a child run.

    Oversized payloads are rejected instead of truncated: a truncated result
    would still hash, and the parent would have no way to tell that it was
    reading a fragment. Bulk content belongs in an Artifact, with the id here.
    """
    if outcome not in CHILD_OUTCOMES:
        raise ValidationError(
            "invalid_child_result",
            f"Unknown child outcome: {outcome}",
            details={"allowed": list(CHILD_OUTCOMES)},
        )
    payload = data if data is not None else {}
    if not isinstance(payload, dict):
        raise ValidationError("invalid_child_result", "Child result data must be a JSON object")
    guard_json_document(payload, label="Child result data", max_bytes=MAX_RESULT_DATA_BYTES)
    reject_unsafe_durable_payload(payload, code=_UNSAFE_CODE, subject=_UNSAFE_SUBJECT)
    reject_secret_material(payload, label="Child result data")
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "outcome": outcome,
        "summary": _validate_summary(summary),
        "data": payload,
        "artifactRefs": _validate_artifact_refs(artifact_refs),
    }
    # Canonicalization is part of validation: a value that cannot be hashed
    # must not become a stored result that nobody can verify later.
    canonical_bytes(document)
    return document


def result_hash(document: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical bytes of the result document."""
    return content_hash(document)
