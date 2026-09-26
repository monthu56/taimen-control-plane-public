"""Journal event -> CloudEvents 1.0 (structured JSON format).

The journal keeps its own envelope (CP-ADR-0068); CloudEvents is an export
for systems that speak it — a relay to a webhook, a bus adapter. Mapping:
``id``, ``type`` and ``occurredAt`` become ``id``, ``type`` and ``time``, the
entity becomes ``subject``, ``payload`` becomes ``data``; the envelope fields
without a CloudEvents counterpart travel as extension attributes (lowercase,
no separators, as the spec requires).
"""

from collections.abc import Mapping
from typing import Any

SPEC_VERSION = "1.0"

# Envelope field -> extension attribute. Absent or null fields are omitted:
# CloudEvents has no null attribute values.
_EXTENSIONS = {
    "tenantId": "tenantid",
    "workspaceId": "workspaceid",
    "entityType": "entitytype",
    "entityId": "entityid",
    "schemaVersion": "schemaversion",
    "actorId": "actorid",
    "correlationId": "correlationid",
    "causationId": "causationid",
}


def default_source(event: Mapping[str, Any]) -> str:
    """``/control-plane/tenants/<tenantId>``: an event id is unique per tenant journal."""
    return f"/control-plane/tenants/{event['tenantId']}"


def to_cloudevent(event: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    """One journal event (as ``GET /events`` returns it) as a CloudEvent.

    ``source`` is a URI-reference naming the producing installation, e.g.
    ``https://cp.example.com/tenants/<id>``; the default is the relative
    :func:`default_source`. ``data`` is the payload under the event's
    ``schemaversion`` in the event catalog (``docs/events/catalog.json``).
    """
    cloudevent: dict[str, Any] = {
        "specversion": SPEC_VERSION,
        "id": str(event["id"]),
        "source": source or default_source(event),
        "type": event["type"],
        "time": event["occurredAt"],
        "subject": f"{event['entityType']}/{event['entityId']}",
        "datacontenttype": "application/json",
    }
    for field, attribute in _EXTENSIONS.items():
        value = event.get(field)
        if value is not None:
            cloudevent[attribute] = value
    cloudevent["data"] = event.get("payload", {})
    return cloudevent
