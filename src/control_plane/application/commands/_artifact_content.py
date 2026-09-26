"""Content fields every writer of ``artifact.created`` puts in its event
(schema v2, CP-ADR-0072 §11). Kept apart from ``artifacts`` so the skill,
rule and verification commands can use it without importing the upload path."""

from typing import Any

from control_plane.infrastructure.db.models import Artifact

CONTENT_NONE = "none"
CONTENT_STORED = "stored"
CONTENT_PURGED = "purged"


def artifact_event_fields(artifact: Artifact) -> dict[str, Any]:
    """Size, media type, checksum and state — never the content itself."""
    return {
        "sizeBytes": artifact.size_bytes,
        "mediaType": artifact.media_type,
        "sha256": artifact.sha256,
        "contentState": artifact.content_state or CONTENT_NONE,
        "typeVersion": artifact.type_version,
    }
