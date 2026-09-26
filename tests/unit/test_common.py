import uuid
from datetime import UTC, datetime

import pytest

from control_plane.application.common import (
    clamp_ttl,
    decode_cursor,
    encode_cursor,
    make_created_cursor,
    parse_created_cursor,
)
from control_plane.domain.errors import ValidationError


def test_cursor_roundtrip() -> None:
    data = {"c": "2026-01-01T00:00:00+00:00", "i": str(uuid.uuid4())}
    assert decode_cursor(encode_cursor(data)) == data


def test_created_cursor_roundtrip() -> None:
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    entity_id = uuid.uuid4()
    cursor = make_created_cursor(created_at, entity_id)
    assert parse_created_cursor(cursor) == (created_at, entity_id)


@pytest.mark.parametrize("bad", ["", "!!!", "bm90LWpzb24", encode_cursor({"x": 1})[:-2] + "zz"])
def test_malformed_cursor_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        parse_created_cursor(bad)


def test_clamp_ttl_default_and_bounds() -> None:
    assert clamp_ttl(None, default=300, minimum=10, maximum=600) == 300
    assert clamp_ttl(60, default=300, minimum=10, maximum=600) == 60
    with pytest.raises(ValidationError):
        clamp_ttl(5, default=300, minimum=10, maximum=600)
    with pytest.raises(ValidationError):
        clamp_ttl(601, default=300, minimum=10, maximum=600)
