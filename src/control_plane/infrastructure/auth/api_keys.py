"""API key material: generation, hashing, parsing.

Format: ``cp_<prefix>_<secret>``. Only the SHA-256 hash is stored; the full key
is returned exactly once, at creation time. Lookup goes by the indexed prefix,
comparison by constant-time hash equality.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

_KEY_TAG = "cp"
# 48 bits of prefix entropy: a birthday collision on the unique index is not a
# realistic event, so key creation never trips over it.
_PREFIX_LEN = 12
# Break-glass keys (ADR-0065) carry a prefix that starts with "bg". An ordinary
# prefix is hex, and "g" is not a hex digit, so the mark cannot occur by chance
# and needs no column of its own.
_BREAK_GLASS_MARK = "bg"


@dataclass(frozen=True)
class GeneratedKey:
    full_key: str
    prefix: str
    key_hash: str


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def generate_api_key() -> GeneratedKey:
    prefix = secrets.token_hex(_PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(32)
    full_key = f"{_KEY_TAG}_{prefix}_{secret}"
    return GeneratedKey(full_key=full_key, prefix=prefix, key_hash=hash_api_key(full_key))


def generate_break_glass_key() -> GeneratedKey:
    prefix = _BREAK_GLASS_MARK + secrets.token_hex((_PREFIX_LEN - len(_BREAK_GLASS_MARK)) // 2)
    secret = secrets.token_urlsafe(32)
    full_key = f"{_KEY_TAG}_{prefix}_{secret}"
    return GeneratedKey(full_key=full_key, prefix=prefix, key_hash=hash_api_key(full_key))


def is_break_glass_prefix(prefix: str | None) -> bool:
    return prefix is not None and prefix.startswith(_BREAK_GLASS_MARK)


def extract_prefix(full_key: str) -> str | None:
    """Return the lookup prefix from a presented key, or None if malformed."""
    parts = full_key.split("_", 2)
    if len(parts) != 3 or parts[0] != _KEY_TAG or len(parts[1]) != _PREFIX_LEN:
        return None
    return parts[1]


def verify_api_key(full_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(full_key), stored_hash)
