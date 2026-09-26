"""Opaque, versioned replay cursor over the event journal.

Public replay order is the pair ``(tx_id, sequence)`` under the *stable
horizon* filter ``tx_id < pg_snapshot_xmin(pg_current_snapshot())``.

Why this is a complete prefix (no committed event can be permanently
skipped):

1. ``tx_id`` is the writer's 64-bit ``xid8``, assigned by PostgreSQL at the
   transaction's first write and monotonically increasing across the cluster
   (``xid8`` never wraps).
2. For a reader's snapshot, every transaction with ``xid < xmin`` has already
   finished: committed rows below the horizon are visible, aborted ones left
   no rows. So the set "events with ``tx_id < xmin``" is *final* — it can
   only grow by events with ``tx_id >= xmin``.
3. Any still-pending transaction has ``tx_id >= xmin``, hence its events sort
   strictly after every position a reader could have been handed out below
   the horizon. A cursor that only ever advances along delivered
   ``(tx_id, sequence)`` positions therefore can never step over an event
   that later commits: that event's position is greater than the cursor.
4. Within one transaction ``sequence`` (assigned at INSERT) gives a
   deterministic intra-transaction order and a total tie-break.

The price of the guarantee: one long-open *writing* transaction pins ``xmin``
and delays delivery of every event with a newer ``tx_id`` until it finishes.
Delivery is delayed, never lost (documented semantics; commands are
single-transaction and short).

``sequence`` alone is NOT a safe replay cursor: it is drawn at INSERT time
while ``tx_id`` is drawn at the transaction's first write, so the two orders
can invert under concurrency and a sequence-valued cursor can move past a
still-invisible lower sequence for good (the v0.3 defect, reproduced by
``test_event_prefix_is_complete_under_xid_inversion``). ``sequence`` remains
the event identifier, a display/audit field and the intra-transaction order.

Wire format
-----------

``ec1_<base64url(JSON)>`` — versioned by the ``ec1`` prefix, URL-safe, no
padding, no secret data. Payloads:

* ``{"t": <tx_id>, "s": <sequence>}`` — a delivered position (exclusive).
* ``{"q": <sequence>}`` — a *legacy floor*: replay continues under the v0.3
  contract "events with sequence > q" until the first delivered event
  upgrades the cursor to a position.

Clients must treat cursors as opaque strings: never compare, order or
construct them. Legacy inputs are still accepted server-side: a bare integer
(v0.3 ``after=<sequence>``) and the v0.3 ``nextCursor`` encoding
(base64url of ``{"s": <sequence>}``) both decode to a legacy floor.
"""

import base64
import binascii
import json
import re
from dataclasses import dataclass

from control_plane.domain.errors import ValidationError

CURSOR_VERSION = 1
_PREFIX = f"ec{CURSOR_VERSION}_"
_VERSIONED = re.compile(r"^ec(\d+)_")


@dataclass(frozen=True, order=True)
class EventPosition:
    """A delivered replay position: strictly ordered by (tx_id, sequence)."""

    tx_id: int
    sequence: int


ORIGIN = EventPosition(0, 0)


@dataclass(frozen=True)
class LegacyFloor:
    """v0.3 cursor semantics: 'the client has consumed sequences <= floor'.

    Replay from a floor keeps the old contract for one stretch: deliver
    committed events with ``sequence > floor`` in (tx_id, sequence) order.
    The first delivered event upgrades the cursor to an
    :class:`EventPosition`, after which the full no-permanent-gap guarantee
    applies. Events the legacy client had already seen may be re-delivered
    during the switch (at-least-once). Events the v0.3 defect skipped BELOW
    the floor are not recoverable through this cursor — that requires an
    explicit replay from the origin.
    """

    floor: int


EventCursor = EventPosition | LegacyFloor


def _b64encode(payload: dict[str, int]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(token: str) -> object:
    padded = token + "=" * (-len(token) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def encode_position(position: EventPosition) -> str:
    return _PREFIX + _b64encode({"t": position.tx_id, "s": position.sequence})


def encode_legacy_floor(floor: int) -> str:
    return _PREFIX + _b64encode({"q": floor})


def encode_cursor(cursor: EventCursor) -> str:
    if isinstance(cursor, EventPosition):
        return encode_position(cursor)
    return encode_legacy_floor(cursor.floor)


def _invalid() -> ValidationError:
    return ValidationError("invalid_cursor", "Malformed event cursor")


def _strict_int(value: object) -> int | None:
    """A JSON integer and nothing else (bool is an int subclass — reject)."""
    if type(value) is int and value >= 0:
        return value
    return None


def decode_cursor(raw: str) -> EventCursor:
    """Decode any accepted cursor input into its server-side meaning.

    Accepts, in order: a bare non-negative integer (v0.3 ``after=``), a
    versioned ``ec<N>_`` cursor, and the v0.3 ``nextCursor`` encoding
    (unprefixed base64url of ``{"s": int}``). Anything else — including
    Unicode digit lookalikes and type-confused payloads — is a clean
    ``invalid_cursor``, never a server error.
    """
    if raw.isdecimal():
        try:
            return LegacyFloor(int(raw))
        except ValueError as exc:  # pragma: no cover - isdecimal guards this
            raise _invalid() from exc
    if raw.isdigit():  # Unicode digits (e.g. "²") pass isdigit but not int()
        raise _invalid()

    match = _VERSIONED.match(raw)
    if match:
        if int(match.group(1)) != CURSOR_VERSION:
            raise ValidationError(
                "unsupported_cursor_version",
                f"Event cursor version ec{match.group(1)} is not supported by this server",
                details={"supported": [f"ec{CURSOR_VERSION}"]},
            )
        try:
            data = _b64decode(raw[len(_PREFIX) :])
        except (binascii.Error, ValueError) as exc:
            raise _invalid() from exc
        if not isinstance(data, dict):
            raise _invalid()
        if set(data) == {"t", "s"}:
            tx_id = _strict_int(data["t"])
            sequence = _strict_int(data["s"])
            if tx_id is not None and sequence is not None:
                return EventPosition(tx_id=tx_id, sequence=sequence)
        if set(data) == {"q"}:
            floor = _strict_int(data["q"])
            if floor is not None:
                return LegacyFloor(floor)
        raise _invalid()

    # v0.3 nextCursor: unprefixed base64url of {"s": <sequence>}.
    try:
        data = _b64decode(raw)
    except (binascii.Error, ValueError) as exc:
        raise _invalid() from exc
    if isinstance(data, dict) and set(data) == {"s"}:
        floor = _strict_int(data["s"])
        if floor is not None:
            return LegacyFloor(floor)
    raise _invalid()
