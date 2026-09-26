import pytest

from control_plane.application.context.assertions import validate_anchors, validate_assertions
from control_plane.domain.errors import ValidationError


def test_entity_and_fact_use_canonical_memory_shapes():
    result = validate_assertions(
        [
            {"assert": "entity", "entity": {"key": "company:example", "type": "company"}},
            {
                "assert": "fact",
                "fact": {
                    "subject": "company:example",
                    "predicate": "HAS_CONTACT",
                    "object": "contact:sales",
                    "confidence": 0.7,
                },
            },
        ]
    )
    assert result[0]["entity"]["properties"] == {}
    assert result[1]["fact"]["confidence"] == 0.7


@pytest.mark.parametrize(
    "value",
    [
        [
            {
                "assert": "entity",
                "entity": {"key": "company:x", "type": "company"},
                "namespace": "foreign",
            }
        ],
        [{"assert": "entity", "entity": {"key": "company:x", "type": "company", "scopes": []}}],
        [{"assert": "fact", "fact": {"subject": "company:x", "predicate": "P", "object": "bad"}}],
        [
            {
                "assert": "fact",
                "fact": {
                    "subject": "company:x",
                    "predicate": "P",
                    "object": "company:y",
                    "confidence": 2,
                },
            }
        ],
        [{"assert": "text", "text": {"content": "x"}}],
        [
            {
                "assert": "entity",
                "entity": {
                    "key": "company:x",
                    "type": "company",
                    "properties": {"blob": "x" * 65536},
                },
            }
        ],
        [{"assert": "entity", "entity": {"key": "company:x", "type": "company"}}] * 201,
    ],
)
def test_invalid_or_authority_changing_assertions_are_rejected(value):
    with pytest.raises(ValidationError):
        validate_assertions(value)


def test_anchors_bounded_and_deduplicated():
    assert validate_anchors(["company:x", "company:x"]) == ["company:x"]
    for value in (["company:x"] * 11, ["bad key"], ["*"]):
        with pytest.raises(ValidationError):
            validate_anchors(value)
