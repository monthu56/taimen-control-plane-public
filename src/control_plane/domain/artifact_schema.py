"""What artifacts a task type version takes in and hands on (CP-ADR-0072 §7).

A task type version may carry an ``artifact_schema``::

    {"inputs":  [{"key", "type", "from", "required"?}],
     "outputs": [{"key", "type", "required"?, "mediaTypes"?, "content"?}]}

An input names an artifact type and the relation of the receiving task to
the task that produced it (``depends_on``, ``spawned_by``, ``parent`` — the
source is the ``to`` end of a relation going out of the receiving task). An
output names an artifact type the task is expected to hand in; turning
required outputs into verification criteria is CP-ADR-0067's business.

Core knows the keys, artifact type keys, relations and media types — never
what an artifact type means. Pure functions, no database and no I/O; whether
the artifact types exist is checked by the command that publishes the version.
"""

import re
from dataclasses import dataclass
from typing import Any

from control_plane.domain.artifact_type import is_media_pattern, pattern_covered
from control_plane.domain.enums import TaskRelationType
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import guard_json_document

INVALID_ARTIFACT_SCHEMA = "invalid_artifact_schema"
UNKNOWN_ARTIFACT_TYPE = "unknown_artifact_type"

ROOT = "artifactSchema"
MAX_ENTRIES = 32
MAX_OUTPUT_MEDIA_TYPES = 50
INPUT_RELATIONS = (
    TaskRelationType.DEPENDS_ON.value,
    TaskRelationType.SPAWNED_BY.value,
    TaskRelationType.PARENT.value,
)
CONTENT_REQUIRED = "required"
CONTENT_OPTIONAL = "optional"

_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,55}$")
# The artifact type key grammar (the `key` field of POST /artifact-types).
TYPE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_INPUT_FIELDS = frozenset({"key", "type", "from", "required"})
_OUTPUT_FIELDS = frozenset({"key", "type", "required", "mediaTypes", "content"})


@dataclass(frozen=True)
class ArtifactInput:
    key: str
    type: str
    from_: str
    required: bool

    def ref(self) -> dict[str, str]:
        return {"key": self.key, "type": self.type, "from": self.from_}


@dataclass(frozen=True)
class ArtifactOutput:
    key: str
    type: str
    required: bool
    media_types: tuple[str, ...] | None
    content: str


@dataclass(frozen=True)
class ArtifactSchema:
    inputs: tuple[ArtifactInput, ...]
    outputs: tuple[ArtifactOutput, ...]

    @property
    def empty(self) -> bool:
        return not self.inputs and not self.outputs

    @property
    def required_inputs(self) -> tuple[ArtifactInput, ...]:
        return tuple(i for i in self.inputs if i.required)

    def artifact_types(self) -> list[str]:
        """Artifact type keys referenced, in order of first mention."""
        keys: list[str] = []
        entries: tuple[ArtifactInput | ArtifactOutput, ...] = (*self.inputs, *self.outputs)
        for entry in entries:
            if entry.type not in keys:
                keys.append(entry.type)
        return keys


EMPTY_ARTIFACT_SCHEMA = ArtifactSchema(inputs=(), outputs=())


def _invalid(message: str, field: str) -> ValidationError:
    return ValidationError(INVALID_ARTIFACT_SCHEMA, message, details={"field": field})


def _entries(document: dict[str, Any], name: str) -> list[tuple[str, dict[str, Any]]]:
    raw = document.get(name, [])
    field = f"{ROOT}.{name}"
    if not isinstance(raw, list):
        raise _invalid(f"{field} must be a list", field)
    if len(raw) > MAX_ENTRIES:
        raise _invalid(f"{field} allows at most {MAX_ENTRIES} entries", field)
    entries: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise _invalid(f"{where} must be an object", where)
        allowed = _INPUT_FIELDS if name == "inputs" else _OUTPUT_FIELDS
        unknown = set(item) - allowed
        if unknown:
            raise _invalid(f"{where}: unknown keys {sorted(unknown)}", where)
        key = item.get("key")
        if not isinstance(key, str) or not _KEY.match(key):
            raise _invalid(f"{where}.key must match {_KEY.pattern}", f"{where}.key")
        if key in seen:
            raise _invalid(f"{where}.key {key!r} is declared twice", f"{where}.key")
        seen.add(key)
        type_key = item.get("type")
        if not isinstance(type_key, str) or not TYPE_KEY_RE.match(type_key):
            raise _invalid(f"{where}.type must be an artifact type key", f"{where}.type")
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise _invalid(f"{where}.required must be a boolean", f"{where}.required")
        entries.append((where, item))
    return entries


def _output_media_types(raw: Any, where: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    field = f"{where}.mediaTypes"
    if not isinstance(raw, list) or not raw:
        raise _invalid(f"{field} must be a non-empty list", field)
    if len(raw) > MAX_OUTPUT_MEDIA_TYPES:
        raise _invalid(f"{field} allows at most {MAX_OUTPUT_MEDIA_TYPES} entries", field)
    normalized: list[str] = []
    for index, item in enumerate(raw):
        value = item.strip().lower() if isinstance(item, str) else None
        if value is None or not is_media_pattern(value):
            raise _invalid(
                f"{field}[{index}] must be 'type/subtype', 'type/*' or '*/*'",
                f"{field}[{index}]",
            )
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def parse_artifact_schema(document: Any) -> ArtifactSchema:
    """Validate an ``artifact_schema`` document; ``{}`` declares nothing.

    Every defect is ``invalid_artifact_schema`` with ``details.field``. The
    artifact types named here are not looked up: see
    :func:`check_against_artifact_types`.
    """
    if document is None or document == {}:
        return EMPTY_ARTIFACT_SCHEMA
    if not isinstance(document, dict):
        raise _invalid(f"{ROOT} must be an object", ROOT)
    try:
        guard_json_document(document, label=ROOT)
    except ValidationError as exc:
        raise _invalid(exc.message, ROOT) from exc
    unknown = set(document) - {"inputs", "outputs"}
    if unknown:
        raise _invalid(f"{ROOT}: unknown keys {sorted(unknown)}", ROOT)

    inputs: list[ArtifactInput] = []
    for where, item in _entries(document, "inputs"):
        source = item.get("from")
        if source not in INPUT_RELATIONS:
            raise _invalid(f"{where}.from must be one of {list(INPUT_RELATIONS)}", f"{where}.from")
        inputs.append(
            ArtifactInput(
                key=item["key"],
                type=item["type"],
                from_=source,
                required=item.get("required", False),
            )
        )

    outputs: list[ArtifactOutput] = []
    for where, item in _entries(document, "outputs"):
        content = item.get("content", CONTENT_REQUIRED)
        if content not in (CONTENT_REQUIRED, CONTENT_OPTIONAL):
            raise _invalid(
                f"{where}.content must be {CONTENT_REQUIRED!r} or {CONTENT_OPTIONAL!r}",
                f"{where}.content",
            )
        outputs.append(
            ArtifactOutput(
                key=item["key"],
                type=item["type"],
                required=item.get("required", False),
                media_types=_output_media_types(item.get("mediaTypes"), where),
                content=content,
            )
        )
    return ArtifactSchema(inputs=tuple(inputs), outputs=tuple(outputs))


def check_against_artifact_types(schema: ArtifactSchema, registered: dict[str, list[str]]) -> None:
    """Every referenced artifact type exists; output media types narrow it.

    ``registered`` maps the key of each artifact type registered in the
    tenant to the media types of its latest version.
    """
    groups: tuple[tuple[str, tuple[ArtifactInput | ArtifactOutput, ...]], ...] = (
        ("inputs", schema.inputs),
        ("outputs", schema.outputs),
    )
    for name, entries in groups:
        for index, entry in enumerate(entries):
            if entry.type not in registered:
                field = f"{ROOT}.{name}[{index}].type"
                raise ValidationError(
                    UNKNOWN_ARTIFACT_TYPE,
                    f"artifact type {entry.type!r} is not registered",
                    details={"field": field, "artifactType": entry.type},
                )
    for index, output in enumerate(schema.outputs):
        allowed = registered[output.type]
        for position, pattern in enumerate(output.media_types or ()):
            if not pattern_covered(allowed, pattern):
                field = f"{ROOT}.outputs[{index}].mediaTypes[{position}]"
                raise ValidationError(
                    INVALID_ARTIFACT_SCHEMA,
                    f"{pattern!r} is not among the media types of {output.type!r}",
                    details={"field": field, "artifactType": output.type, "allowed": allowed},
                )


def schema_of(document: Any) -> ArtifactSchema:
    """The schema of an already-published (hence valid) document."""
    return parse_artifact_schema(dict(document or {}))
