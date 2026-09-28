"""Decision tables: cells, hit policies, overlaps and gaps (FR-004; process-packages P006)."""

from datetime import UTC, date, datetime
from typing import Any

import pytest

from control_plane.domain import decision_table as dt
from control_plane.domain.decision_table import ANY, DecisionError, OneOf, Range


def _table(
    rules: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    policy: str = "first",
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    inputs = inputs if inputs is not None else {"amount": "number"}
    outputs = outputs if outputs is not None else {"level": "number"}
    return {
        "id": "t",
        "hitPolicy": policy,
        "inputs": [
            {"id": name, "expr": f"data.{name}", "type": kind} for name, kind in inputs.items()
        ],
        "outputs": [{"id": name, "type": kind} for name, kind in outputs.items()],
        "rules": [{"when": when, "then": then} for when, then in rules],
    }


def _build(spec: dict[str, Any]) -> dt.Table:
    table, findings = dt.build(spec, [item["type"] for item in spec["inputs"]])
    assert findings == []
    return table


def _codes(spec: dict[str, Any]) -> list[tuple[str, int | None]]:
    return [(f.code, f.rule) for f in dt.check(_build(spec))]


# --- cells ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "kind", "condition"),
    [
        ("-", "number", ANY),
        ("", "string", ANY),
        (None, "boolean", ANY),
        ("go", "string", OneOf(frozenset({"go"}))),
        ("'go', no-go", "string", OneOf(frozenset({"go", "no-go"}))),
        (10, "number", OneOf(frozenset({10.0}))),
        ("1,2", "number", OneOf(frozenset({1.0, 2.0}))),
        (True, "boolean", OneOf(frozenset({True}))),
        ("false", "boolean", OneOf(frozenset({False}))),
        ("[0..10)", "number", Range(0.0, True, 10.0, False)),
        ("(0..]", "number", Range(0.0, False, None, False)),
        (">=5", "number", Range(5.0, True, None, False)),
        ("<5", "number", Range(None, False, 5.0, False)),
        # Over whole days a range is kept closed: (Jan 1..Jan 5) is [Jan 2..Jan 4].
        (
            "(2026-01-01..2026-01-05)",
            "date",
            Range(
                float(date(2026, 1, 2).toordinal()), True, float(date(2026, 1, 4).toordinal()), True
            ),
        ),
    ],
)
def test_a_cell_is_any_a_literal_a_list_or_a_range(cell: Any, kind: str, condition: Any) -> None:
    assert dt.parse_condition(cell, kind) == condition


@pytest.mark.parametrize(
    ("cell", "kind"),
    [
        ("[ten..20)", "number"),
        ("[5..1]", "number"),
        ("maybe", "boolean"),
        ("2026-13-01", "date"),
        ("[2026-01-01T00:00:00..)", "timestamp"),
        ("(1..1)", "number"),
    ],
)
def test_a_cell_that_is_not_a_condition_of_its_type_is_a_finding(cell: str, kind: str) -> None:
    with pytest.raises(DecisionError) as raised:
        dt.parse_condition(cell, kind)
    assert raised.value.code == "invalid_table_cell"
    assert raised.value.hint


def test_the_table_names_unknown_inputs_outputs_and_missing_outputs() -> None:
    spec = _table([({"amont": "1"}, {"levl": 1})])
    _, findings = dt.build(spec, ["number"])
    assert [(f.code, f.rule, f.input or f.output) for f in findings] == [
        ("unknown_table_input", 0, "amont"),
        ("unknown_table_output", 0, "levl"),
        ("table_output_missing", 0, "level"),
    ]
    assert findings[0].hint == "did you mean amount?"
    _, findings = dt.build(_table([({}, {"level": "high"})]), ["number"])
    assert [f.code for f in findings] == ["table_output_type_mismatch"]


# --- overlaps, unreachable rules and gaps --------------------------------------------


def test_rules_of_a_unique_table_must_not_overlap() -> None:
    spec = _table(
        [({"amount": "<500"}, {"level": 1}), ({"amount": ">=500"}, {"level": 2})], policy="unique"
    )
    assert _codes(spec) == []
    spec["rules"][1]["when"]["amount"] = ">=400"
    assert _codes(spec) == [("table_overlap", 1)]


def test_a_first_rule_covered_by_earlier_ones_is_unreachable() -> None:
    spec = _table(
        [
            ({"amount": "<100"}, {"level": 1}),
            ({"amount": ">=100"}, {"level": 2}),
            ({"amount": "[50..150]"}, {"level": 3}),  # covered by the two together
            ({"amount": "-"}, {"level": 4}),
        ]
    )
    assert _codes(spec) == [("table_rule_unreachable", 2), ("table_rule_unreachable", 3)]


def test_a_gap_is_reported_with_an_input_no_rule_matches() -> None:
    spec = _table(
        [({"amount": "<0"}, {"level": 0}), ({"amount": "(0..1000)"}, {"level": 1})],
        policy="unique",
    )
    findings = dt.check(_build(spec))
    assert [f.code for f in findings] == ["table_gap"]
    assert findings[0].severity == "warning"
    assert "amount = 0" in findings[0].message
    # A collect table may match nothing: an empty list is its answer.
    spec["hitPolicy"] = "collect"
    assert _codes(spec) == []


def test_gaps_over_several_inputs_strings_and_booleans() -> None:
    inputs = {"kind": "string", "urgent": "boolean"}
    rules: list[tuple[dict[str, Any], dict[str, Any]]] = [
        ({"kind": "goods,works", "urgent": True}, {"level": 1}),
        ({"kind": "goods,works", "urgent": False}, {"level": 2}),
        ({"kind": "services"}, {"level": 3}),
    ]
    findings = dt.check(_build(_table(rules, inputs=inputs)))
    assert [f.code for f in findings] == ["table_gap"]
    assert "kind = any other value" in findings[0].message
    rules.append(({}, {"level": 4}))
    assert _codes(_table(rules, inputs=inputs)) == []


def test_dates_are_whole_days() -> None:
    spec = _table(
        [
            ({"day": "<=2026-01-01"}, {"level": 1}),
            ({"day": "(2026-01-01..2026-01-02]"}, {"level": 2}),
            ({"day": ">2026-01-02"}, {"level": 3}),
        ],
        policy="unique",
        inputs={"day": "date"},
    )
    assert _codes(spec) == []  # no day between Jan 1 and Jan 2


def test_a_table_too_large_to_search_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dt, "SEARCH_BUDGET", 3)
    spec = _table(
        [
            ({"a": "<0"}, {"level": 1}),
            ({"a": ">=0", "b": "<0"}, {"level": 2}),
            ({"a": ">=0", "b": ">=0"}, {"level": 3}),
        ],
        policy="unique",
        inputs={"a": "number", "b": "number"},
    )
    assert _codes(spec) == [("table_check_truncated", None)]


# --- evaluation ------------------------------------------------------------------------


def test_first_takes_the_first_match() -> None:
    table = _build(_table([({"amount": "<100"}, {"level": 1}), ({"amount": "-"}, {"level": 2})]))
    assert dt.evaluate(table, {"amount": 50}) == dt.Decision({"level": 1}, (0,))
    assert dt.evaluate(table, {"amount": 150}) == dt.Decision({"level": 2}, (1,))
    # A value of another type (or null) matches only "-".
    assert dt.evaluate(table, {"amount": "a lot"}).rules == (1,)
    assert dt.evaluate(table, {}).rules == (1,)


def test_unique_refuses_no_match_and_two_matches() -> None:
    table = _build(
        _table(
            [({"amount": "<=100"}, {"level": 1}), ({"amount": ">=100"}, {"level": 2})],
            policy="unique",
        )
    )
    assert dt.evaluate(table, {"amount": 10}).result == {"level": 1}
    with pytest.raises(DecisionError) as raised:
        dt.evaluate(table, {"amount": 100})
    assert raised.value.code == "decision_ambiguous"
    with pytest.raises(DecisionError) as raised:
        dt.evaluate(table, {"amount": None})
    assert raised.value.code == "decision_no_match"


def test_collect_gives_every_match_in_order() -> None:
    table = _build(
        _table(
            [
                ({"amount": ">=10"}, {"level": 1}),
                ({"amount": ">=100"}, {"level": 2}),
                ({"amount": "<0"}, {"level": 3}),
            ],
            policy="collect",
        )
    )
    assert dt.evaluate(table, {"amount": 500}) == dt.Decision([{"level": 1}, {"level": 2}], (0, 1))
    assert dt.evaluate(table, {"amount": 5}) == dt.Decision([], ())


def test_timestamps_and_dates_from_their_json_forms() -> None:
    table = _build(
        _table(
            [({"at": "<2026-01-01T00:00:00Z"}, {"level": 1}), ({}, {"level": 2})],
            inputs={"at": "timestamp"},
        )
    )
    assert dt.evaluate(table, {"at": "2025-12-31T23:59:59+00:00"}).rules == (0,)
    assert dt.evaluate(table, {"at": datetime(2026, 1, 1, tzinfo=UTC)}).rules == (1,)
    table = _build(
        _table(
            [({"day": "2026-01-01,2026-01-07"}, {"level": 1}), ({}, {"level": 2})],
            inputs={"day": "date"},
        )
    )
    assert dt.evaluate(table, {"day": "2026-01-07"}).rules == (0,)
    assert dt.evaluate(table, {"day": date(2026, 1, 2)}).rules == (1,)
