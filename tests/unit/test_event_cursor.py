"""Unit tests for the opaque event cursor codec."""

import pytest

from control_plane.application.event_cursor import (
    EventPosition,
    LegacyFloor,
    decode_cursor,
    encode_cursor,
    encode_legacy_floor,
    encode_position,
)
from control_plane.domain.errors import ValidationError


def test_position_roundtrip() -> None:
    position = EventPosition(tx_id=123456789, sequence=42)
    encoded = encode_position(position)
    assert encoded.startswith("ec1_")
    assert decode_cursor(encoded) == position


def test_legacy_floor_roundtrip() -> None:
    encoded = encode_legacy_floor(17)
    assert decode_cursor(encoded) == LegacyFloor(17)


def test_encode_cursor_dispatches() -> None:
    assert decode_cursor(encode_cursor(EventPosition(1, 2))) == EventPosition(1, 2)
    assert decode_cursor(encode_cursor(LegacyFloor(3))) == LegacyFloor(3)


def test_positions_order_by_tx_then_sequence() -> None:
    assert EventPosition(1, 100) < EventPosition(2, 1)
    assert EventPosition(2, 1) < EventPosition(2, 2)


def test_bare_integer_is_legacy_floor() -> None:
    assert decode_cursor("0") == LegacyFloor(0)
    assert decode_cursor("1234") == LegacyFloor(1234)


def test_v03_next_cursor_encoding_is_legacy_floor() -> None:
    import base64

    legacy = base64.urlsafe_b64encode(b'{"s": 55}').decode().rstrip("=")
    assert decode_cursor(legacy) == LegacyFloor(55)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ec1_",
        "ec1_!!!",
        "ec1_e30",  # {}
        "ec1_eyJ0IjoxfQ",  # {"t": 1} — missing "s"
        "ec1_eyJ0IjoiYSIsInMiOjF9",  # {"t": "a", "s": 1} — wrong type
        "ec1_eyJ0IjotMSwicyI6MX0",  # {"t": -1, "s": 1} — negative
        "not/base64",
        "-5",
        "\u00b2",  # Unicode superscript two: isdigit() but not int()
        "ec1_eyJ0Ijp0cnVlLCJzIjoxfQ",  # {"t": true, "s": 1} — bool is not int
    ],
)
def test_malformed_cursors_rejected(raw: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        decode_cursor(raw)
    assert excinfo.value.code == "invalid_cursor"


def test_unsupported_version_rejected_distinctly() -> None:
    with pytest.raises(ValidationError) as excinfo:
        decode_cursor("ec99_AAAA")
    assert excinfo.value.code == "unsupported_cursor_version"


def test_cursor_is_url_safe() -> None:
    encoded = encode_position(EventPosition(2**62, 2**62))
    assert "+" not in encoded and "/" not in encoded and "=" not in encoded
