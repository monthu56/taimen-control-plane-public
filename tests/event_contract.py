"""The journal against the event catalog (CP-ADR-0068).

Every event the core wrote must name a (type, schemaVersion) the catalog knows
and carry a payload valid under that version's schema. The integration package
checks this after every test, so the whole suite is the contract test: a new
event type, or a payload key a schema forgot, fails where it is written.
"""

from typing import Any

import jsonschema
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.domain.event_catalog import schema_for

_VALIDATORS: dict[tuple[str, int], jsonschema.Draft202012Validator] = {}


def _validator(event_type: str, version: int) -> jsonschema.Draft202012Validator | None:
    key = (event_type, version)
    if key not in _VALIDATORS:
        schema = schema_for(event_type, version)
        if schema is None:
            return None
        _VALIDATORS[key] = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
        )
    return _VALIDATORS[key]


def payload_violations(event_type: str, version: int, payload: dict[str, Any]) -> list[str]:
    validator = _validator(event_type, version)
    if validator is None:
        return [f"{event_type} v{version}: not in the event catalog"]
    return [
        f"{event_type} v{version}: {'/'.join(map(str, e.absolute_path)) or '<payload>'}: "
        f"{e.message}"
        for e in validator.iter_errors(payload)
    ]


def journal_violations(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type, schema_version, payload FROM events"
                " UNION ALL SELECT event_type, schema_version, payload FROM event_archive"
            )
        ).all()
    found: list[str] = []
    for event_type, version, payload in rows:
        for violation in payload_violations(event_type, version, payload):
            if violation not in found:
                found.append(violation)
    return found
