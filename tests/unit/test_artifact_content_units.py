"""Pure pieces of the artifact content path (CP-ADR-0072 §2, §5)."""

import hashlib
import os
import tempfile
from collections.abc import AsyncIterator

import pytest

from control_plane.api.v1.artifact_contents import normalize_media_type
from control_plane.api.v1.artifacts import content_disposition, is_active_content
from control_plane.domain.errors import BadRequestError
from control_plane.infrastructure.content_store import SpoolLimitExceeded, spool


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _spool_files() -> set[str]:
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("cp-artifact-")}


async def test_spool_hashes_and_counts_on_the_way() -> None:
    spooled = await spool(_chunks(b"ab", b"", b"cd"), limit=4)
    try:
        assert spooled.size == 4
        assert spooled.sha256 == hashlib.sha256(b"abcd").hexdigest()
        assert spooled.path.read_bytes() == b"abcd"
    finally:
        spooled.remove()
    assert not spooled.path.exists()
    spooled.remove()  # twice is fine


async def test_spool_over_the_limit_leaves_nothing() -> None:
    before = _spool_files()
    with pytest.raises(SpoolLimitExceeded):
        await spool(_chunks(b"abc", b"de"), limit=4)
    assert _spool_files() <= before


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("application/pdf", "application/pdf"),
        ("Text/Markdown; charset=UTF-8", "text/markdown; charset=UTF-8"),
        ("  image/svg+xml  ", "image/svg+xml"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", None),
    ],
)
def test_media_type_is_normalized(header: str, expected: str | None) -> None:
    assert normalize_media_type(header) == (expected or header)


@pytest.mark.parametrize("header", [None, "", "pdf", "text/", "/plain", "a b/c", "x/" + "y" * 300])
def test_media_type_is_required(header: str | None) -> None:
    with pytest.raises(BadRequestError) as caught:
        normalize_media_type(header)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize(
    ("media_type", "active"),
    [
        ("text/html", True),
        ("TEXT/HTML; charset=utf-8", True),
        ("application/xhtml+xml", True),
        ("image/svg+xml", True),
        ("application/atom+xml", True),
        ("text/xml", True),
        ("application/javascript", True),
        ("text/javascript", True),
        ("text/plain", False),
        ("application/pdf", False),
        ("application/json", False),
        ("image/png", False),
    ],
)
def test_active_content(media_type: str, active: bool) -> None:
    assert is_active_content(media_type) is active


def test_content_disposition_encodes_the_name() -> None:
    assert content_disposition('a"b;c.pdf', "application/pdf") == (
        "inline; filename*=UTF-8''a%22b%3Bc.pdf"
    )
    assert content_disposition("x.svg", "image/svg+xml").startswith("attachment;")
