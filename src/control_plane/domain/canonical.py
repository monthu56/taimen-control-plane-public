"""Canonical JSON representation and content hashing.

Extracted from the Effective Harness Manifest (HRS-2) when a second consumer
appeared: the scoped tool discovery view (HRS-3) hashes catalog and policy
revisions, and two hash implementations would eventually disagree about what
"the same content" means.

The rules exist to make "same inputs → same bytes" true across processes and
languages: sorted keys, no insignificant whitespace, NFC-normalized strings,
UTF-8, no floats, no exotic types. Anything unstable is rejected at write time
rather than producing a value that hashes differently somewhere else.
"""

import hashlib
import json
import unicodedata
from typing import Any

from control_plane.domain.errors import ValidationError
from control_plane.domain.project import MAX_JSON_DEPTH

HASH_ALGORITHM = "sha256"
MAX_STRING_CHARS = 2_000


def canonicalize(value: Any, *, path: str, depth: int = 1) -> Any:
    """Return a canonical clone of ``value`` or raise on anything unstable."""
    if depth > MAX_JSON_DEPTH:
        raise ValidationError(
            "payload_too_deep",
            f"Document nesting exceeds the {MAX_JSON_DEPTH}-level limit",
            details={"path": path, "maxDepth": MAX_JSON_DEPTH},
        )
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        # 0.1 and 1e-1 are one value with two representations, and float repr
        # is not portable across languages. Neither a manifest nor a tool
        # revision has any use for fractional numbers, so forbidding them
        # removes a whole class of cross-language hash mismatch instead of
        # papering over it.
        raise ValidationError(
            "non_canonical_value",
            "Floating point numbers are not allowed in a canonical document",
            details={"path": path},
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise ValidationError(
                "payload_too_large",
                f"String exceeds the {MAX_STRING_CHARS}-character limit",
                details={"path": path, "maxChars": MAX_STRING_CHARS},
            )
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        canonical: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValidationError(
                    "non_canonical_value",
                    "Object keys must be strings",
                    details={"path": path, "key": repr(raw_key)[:100]},
                )
            key = unicodedata.normalize("NFC", raw_key)
            if key in canonical:
                # Two distinct keys that normalize to the same string would
                # make the canonical form depend on dict ordering.
                raise ValidationError(
                    "non_canonical_value",
                    "Object keys collide after Unicode normalization",
                    details={"path": path, "key": key},
                )
            canonical[key] = canonicalize(item, path=f"{path}.{key}", depth=depth + 1)
        return canonical
    if isinstance(value, (list, tuple)):
        return [
            canonicalize(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise ValidationError(
        "non_canonical_value",
        f"Value of type {type(value).__name__} cannot appear in a canonical document",
        details={"path": path},
    )


def canonical_bytes(value: Any) -> bytes:
    """Byte-stable representation: sorted keys, no whitespace, NFC, UTF-8."""
    canonical = canonicalize(value, path="$")
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    """``sha256:<hex>`` over the canonical bytes.

    The algorithm travels inside the value so a future change is a visible
    schema change rather than a silent reinterpretation of old hashes.
    """
    digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"
