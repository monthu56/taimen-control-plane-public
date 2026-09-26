"""Artifact type definition and the check of an artifact (CP-ADR-0072 §6)."""

import pytest

from control_plane.domain.artifact_type import (
    ArtifactTypeDefinition,
    check_artifact_against_type,
    media_type_allowed,
    validate_artifact_type_definition,
)
from control_plane.domain.errors import ValidationError

GLOBAL = 1000


def definition(**overrides: object) -> ArtifactTypeDefinition:
    values: dict[str, object] = {
        "metadata_schema": {"type": "object", "required": ["n"]},
        "media_types": ["application/pdf", "image/*"],
        "max_bytes": 100,
        "global_max_bytes": GLOBAL,
    }
    values.update(overrides)
    return validate_artifact_type_definition(**values)  # type: ignore[arg-type]


def test_media_types_are_normalized_and_deduplicated() -> None:
    parsed = definition(media_types=["Application/PDF", "application/pdf", "*/*"])
    assert parsed.media_types == ["application/pdf", "*/*"]


def test_max_bytes_omitted_is_the_global_ceiling() -> None:
    assert definition(max_bytes=None).max_bytes == GLOBAL


@pytest.mark.parametrize(
    ("patterns", "media_type", "allowed"),
    [
        (["application/pdf"], "application/pdf", True),
        (["application/pdf"], "Application/PDF; charset=binary", True),
        (["application/pdf"], "application/json", False),
        (["image/*"], "image/png", True),
        (["image/*"], "text/plain", False),
        (["*/*"], "anything/at-all", True),
    ],
)
def test_media_type_patterns(patterns: list[str], media_type: str, allowed: bool) -> None:
    assert media_type_allowed(patterns, media_type) is allowed


def test_check_passes_a_conforming_artifact() -> None:
    check_artifact_against_type(
        definition(),
        key="invoice",
        version=1,
        metadata={"n": 1},
        media_type="image/png",
        size_bytes=100,
    )


def test_check_rejects_metadata_first() -> None:
    with pytest.raises(ValidationError) as caught:
        check_artifact_against_type(
            definition(),
            key="invoice",
            version=1,
            metadata={},
            media_type="text/plain",
            size_bytes=10_000,
        )
    assert caught.value.code == "invalid_artifact_metadata"
    assert caught.value.details["errors"][0]["path"] == "/"


def test_check_rejects_a_media_type_outside_the_type() -> None:
    with pytest.raises(ValidationError) as caught:
        check_artifact_against_type(
            definition(),
            key="invoice",
            version=3,
            metadata={"n": 1},
            media_type="text/html",
            size_bytes=1,
        )
    assert caught.value.code == "media_type_not_allowed"
    assert caught.value.details == {
        "artifactType": "invoice",
        "artifactTypeVersion": 3,
        "mediaType": "text/html",
        "allowed": ["application/pdf", "image/*"],
    }


def test_check_rejects_content_over_max_bytes() -> None:
    with pytest.raises(ValidationError) as caught:
        check_artifact_against_type(
            definition(),
            key="invoice",
            version=1,
            metadata={"n": 1},
            media_type="application/pdf",
            size_bytes=101,
        )
    assert caught.value.code == "artifact_too_large"
    assert caught.value.details["maxBytes"] == 100


def test_without_content_only_metadata_is_checked() -> None:
    check_artifact_against_type(
        definition(),
        key="invoice",
        version=1,
        metadata={"n": 1},
        media_type=None,
        size_bytes=None,
    )
