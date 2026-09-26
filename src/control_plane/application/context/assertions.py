"""Bounded Memory assertions; authority fields belong to Control Plane."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from control_plane.domain.errors import ValidationError

EntityKey = Annotated[
    str, Field(min_length=3, max_length=256, pattern=r"^[a-z][a-z0-9._-]*:[^\s]+$")
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Entity(_Strict):
    key: EntityKey
    type: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
    title: str = Field(default="", max_length=2000)
    properties: dict[str, JsonValue] = Field(default_factory=dict)


class Fact(_Strict):
    subject: EntityKey
    predicate: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9._-]*$")
    object: EntityKey
    confidence: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)


class EntityAssertion(_Strict):
    assertion_kind: Literal["entity"] = Field(alias="assert")
    entity: Entity


class FactAssertion(_Strict):
    assertion_kind: Literal["fact"] = Field(alias="assert")
    fact: Fact


_ASSERTIONS: TypeAdapter[list[EntityAssertion | FactAssertion]] = TypeAdapter(
    Annotated[
        list[Annotated[EntityAssertion | FactAssertion, Field(discriminator="assertion_kind")]],
        Field(max_length=200),
    ]
)
_ANCHORS: TypeAdapter[list[EntityKey]] = TypeAdapter(
    Annotated[list[EntityKey], Field(max_length=10)]
)


def validate_assertions(value: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    try:
        items = _ASSERTIONS.validate_python(value or [])
        encoded = _ASSERTIONS.dump_json(items, by_alias=True)
        if len(encoded) > 65_536:
            raise ValidationError("observation_invalid", "assertions exceed 65536 bytes")
        dumped: list[dict[str, Any]] = _ASSERTIONS.dump_python(items, mode="json", by_alias=True)
        return dumped
    except PydanticValidationError as exc:
        # Do not echo property values (possibly sensitive) in an error response.
        raise ValidationError("observation_invalid", "invalid entity/fact assertions") from exc


def validate_anchors(value: list[str] | None) -> list[str]:
    try:
        return list(dict.fromkeys(_ANCHORS.validate_python(value or [])))
    except PydanticValidationError as exc:
        raise ValidationError("invalid_context_request", "invalid entity anchors") from exc
