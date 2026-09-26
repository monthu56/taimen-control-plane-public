"""The artifactSchema grammar of a task type version (CP-ADR-0072 §7)."""

from typing import Any

import pytest

from control_plane.domain.artifact_schema import (
    EMPTY_ARTIFACT_SCHEMA,
    MAX_ENTRIES,
    check_against_artifact_types,
    parse_artifact_schema,
)
from control_plane.domain.artifact_type import pattern_covered
from control_plane.domain.errors import ValidationError


def entry(**overrides: Any) -> dict[str, Any]:
    return {"key": "a", "type": "doc", "from": "parent", **overrides}


def refused(document: Any) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        parse_artifact_schema(document)
    assert caught.value.code == "invalid_artifact_schema"
    return caught.value


def test_empty_document_declares_nothing() -> None:
    assert parse_artifact_schema({}) is EMPTY_ARTIFACT_SCHEMA
    assert parse_artifact_schema(None).empty
    assert parse_artifact_schema({"inputs": [], "outputs": []}).empty


def test_defaults_and_normalization() -> None:
    schema = parse_artifact_schema(
        {
            "inputs": [{"key": "a", "type": "doc", "from": "parent"}],
            "outputs": [
                {"key": "b", "type": "doc", "mediaTypes": ["Text/Markdown", "text/markdown"]}
            ],
        }
    )
    (entry,) = schema.inputs
    assert (entry.required, entry.from_) == (False, "parent")
    assert schema.required_inputs == ()
    (output,) = schema.outputs
    assert (output.required, output.content, output.media_types) == (
        False,
        "required",
        ("text/markdown",),
    )
    assert schema.artifact_types() == ["doc"]


@pytest.mark.parametrize(
    ("document", "field"),
    [
        ([], "artifactSchema"),
        ({"inputs": {}}, "artifactSchema.inputs"),
        ({"inputs": ["x"]}, "artifactSchema.inputs[0]"),
        ({"inputs": [{"key": "a", "type": "doc"}]}, "artifactSchema.inputs[0].from"),
        ({"inputs": [entry(**{"from": "blocks"})]}, "artifactSchema.inputs[0].from"),
        ({"inputs": [entry(key="-a")]}, "artifactSchema.inputs[0].key"),
        ({"inputs": [entry(key="a" * 57)]}, "artifactSchema.inputs[0].key"),
        ({"inputs": [entry(type="Doc")]}, "artifactSchema.inputs[0].type"),
        ({"inputs": [entry(required="yes")]}, "artifactSchema.inputs[0].required"),
        ({"inputs": [entry(mediaTypes=["*/*"])]}, "artifactSchema.inputs[0]"),
        ({"outputs": [{"key": "a", "type": "doc", "from": "parent"}]}, "artifactSchema.outputs[0]"),
        ({"outputs": [{"key": "a", "type": "doc"}, {"key": "a", "type": "doc"}]},
         "artifactSchema.outputs[1].key"),
        ({"outputs": [{"key": "a", "type": "doc", "mediaTypes": []}]},
         "artifactSchema.outputs[0].mediaTypes"),
        ({"outputs": [{"key": "a", "type": "doc", "mediaTypes": ["pdf"]}]},
         "artifactSchema.outputs[0].mediaTypes[0]"),
    ],
)  # fmt: skip
def test_grammar_violations_name_the_field(document: Any, field: str) -> None:
    assert refused(document).details["field"] == field


def test_same_key_may_be_an_input_and_an_output() -> None:
    schema = parse_artifact_schema(
        {
            "inputs": [{"key": "plan", "type": "doc", "from": "depends_on", "required": True}],
            "outputs": [{"key": "plan", "type": "doc", "content": "optional"}],
        }
    )
    assert [i.key for i in schema.required_inputs] == ["plan"]
    assert schema.outputs[0].content == "optional"


def test_list_length_is_bounded() -> None:
    entries = [{"key": f"k{i}", "type": "doc", "from": "parent"} for i in range(MAX_ENTRIES + 1)]
    assert refused({"inputs": entries}).details["field"] == "artifactSchema.inputs"


def test_referenced_types_must_be_registered_and_outputs_narrow_them() -> None:
    schema = parse_artifact_schema(
        {
            "inputs": [{"key": "a", "type": "doc", "from": "parent"}],
            "outputs": [{"key": "b", "type": "img", "mediaTypes": ["image/png"]}],
        }
    )
    check_against_artifact_types(schema, {"doc": ["*/*"], "img": ["image/*"]})

    with pytest.raises(ValidationError) as caught:
        check_against_artifact_types(schema, {"img": ["image/*"]})
    assert caught.value.code == "unknown_artifact_type"
    assert caught.value.details["field"] == "artifactSchema.inputs[0].type"

    with pytest.raises(ValidationError) as caught:
        check_against_artifact_types(schema, {"doc": ["*/*"], "img": ["image/jpeg"]})
    assert caught.value.code == "invalid_artifact_schema"
    assert caught.value.details["field"] == "artifactSchema.outputs[0].mediaTypes[0]"


@pytest.mark.parametrize(
    ("patterns", "pattern", "covered"),
    [
        (["text/*"], "text/markdown", True),
        (["text/markdown"], "text/*", False),
        (["text/*"], "text/*", True),
        (["*/*"], "text/*", True),
        (["*/*"], "*/*", True),
        (["text/*"], "*/*", False),
        (["application/pdf"], "application/pdf", True),
        (["application/pdf"], "image/png", False),
    ],
)
def test_pattern_coverage(patterns: list[str], pattern: str, covered: bool) -> None:
    assert pattern_covered(patterns, pattern) is covered
