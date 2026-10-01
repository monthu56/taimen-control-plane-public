"""Small shared helpers for the application layer."""

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from control_plane import sandbox
from control_plane.domain.errors import ValidationError


def utcnow() -> datetime:
    """Now; inside a package test, the virtual clock of the test (CP-ADR-0074 Z2)."""
    return sandbox.virtual_now() or datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def clamp_ttl(requested: int | None, *, default: int, minimum: int, maximum: int) -> int:
    """Clamp a client-requested lease TTL into the configured bounds."""
    if requested is None:
        return default
    if requested < minimum or requested > maximum:
        raise ValidationError(
            "invalid_ttl",
            f"ttlSeconds must be between {minimum} and {maximum}",
            details={"min": minimum, "max": maximum},
        )
    return requested


def encode_cursor(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
    if not isinstance(data, dict):
        raise ValidationError("invalid_cursor", "Malformed pagination cursor")
    return data


def parse_created_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    data = decode_cursor(cursor)
    try:
        created_at = datetime.fromisoformat(data["c"])
        entity_id = uuid.UUID(data["i"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
    return created_at, entity_id


def make_created_cursor(created_at: datetime, entity_id: uuid.UUID) -> str:
    return encode_cursor({"c": created_at.isoformat(), "i": str(entity_id)})


def parse_thread_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Cursor of an OLDEST-FIRST page (a discussion thread reads forward).

    Its own key (``a``) for the same reason the date cursor has one: a cursor
    issued under newest-first ordering would decode here and silently paginate
    the other direction, skipping and repeating rows instead of failing.
    """
    data = decode_cursor(cursor)
    try:
        created_at = datetime.fromisoformat(data["a"])
        entity_id = uuid.UUID(data["i"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
    return created_at, entity_id


def make_thread_cursor(created_at: datetime, entity_id: uuid.UUID) -> str:
    return encode_cursor({"a": created_at.isoformat(), "i": str(entity_id)})


def parse_date_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID]:
    """Cursor of a date-ordered page; ``None`` means "in the NULL tail".

    A different key (``d``, not ``c``) from the created-at cursor on purpose:
    a cursor issued under one ordering must not silently paginate another one,
    where it would skip and duplicate rows. Mixing them is ``invalid_cursor``.
    """
    data = decode_cursor(cursor)
    try:
        raw = data["d"]
        value = None if raw is None else datetime.fromisoformat(raw)
        entity_id = uuid.UUID(data["i"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
    return value, entity_id


def make_date_cursor(value: datetime | None, entity_id: uuid.UUID) -> str:
    return encode_cursor({"d": value.isoformat() if value else None, "i": str(entity_id)})
