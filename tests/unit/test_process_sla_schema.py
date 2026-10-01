"""The language of process deadlines in the schemas (CP-ADR-0078 §1, §2, §7; P003).

Only the shape: every form of ``due`` of CP-ADR-0078 §1 is accepted where a
deadline may stand — ``human``, ``approve``, ``call`` (a child process too),
``recall``, ``listen`` and ``spec.due`` — by the pinned catalog schema and by
the core's copy of it; ``wait`` takes no deadline. A calendar may declare its
working hours, a package test may expect SLA states. What the forms mean is
the engine's business, not this schema's.
"""

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from control_plane.api.v1.schemas import CalendarPublishRequest
from control_plane.domain import process_definition as pd
from control_plane.domain.package_source import TEST_SCHEMA_FILE
from tests.unit.test_process_contract import (
    CALENDAR,
    CATALOG,
    PACKAGE_TEST,
    PINNED,
    PROCESS,
    TESTS,
    _yaml12_loader,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"

# CP-ADR-0078 §1, the forms one by one.
DUE_FORMS: list[Any] = [
    "P2D",
    {"at": "cal.addWorkdays(data.deadline, -2)"},
    {"workdays": 2},
    {"workhours": 8, "calendar": "ru"},
    {"duration": "PT4H", "warnBefore": "PT1H"},
    {"workhours": 8, "warnBefore": {"workhours": 2}},
    {"workdays": 5, "warnBefore": {"workdays": 1}},
    {"workhours": 0.5},
]

BAD_DUE_FORMS: list[Any] = [
    "2 days",
    {},
    {"calendar": "ru"},
    {"warnBefore": "PT1H"},
    {"workdays": 2, "workhours": 8},
    {"duration": "PT4H", "workdays": 1},
    {"at": "data.deadline", "warnBefore": "PT1H"},
    {"workdays": 0},
    {"workdays": 1.5},
    {"workhours": 0},
    {"workhours": -1},
    {"duration": "4h"},
    {"workdays": 2, "calendar": "RU"},
    {"workdays": 2, "warnBefore": {"workdays": 1, "workhours": 2}},
    {"workdays": 2, "warnBefore": {"at": "data.deadline"}},
    {"workdays": 2, "sla": "P1D"},
]


def _step(step_id: str, kind: str, body: Any) -> dict[str, Any]:
    return {"id": step_id, kind: body}


def _with_due(due: Any) -> list[tuple[str, dict[str, Any]]]:
    """The places a deadline may stand, each as a whole spec of the example process."""
    places = {
        "human": _step("h", "human", {"taskType": "review", "assign": [{"role": "lead"}]}),
        "approve": _step("a", "approve", {"approvers": [{"role": "lead"}], "quorum": "all"}),
        "call-skill": _step("c", "call", {"skill": "text.summarize@1"}),
        "call-process": _step("p", "call", {"process": "child"}),
        "recall": _step("r", "recall", {"anchors": [{"case": True}]}),
        "listen": _step("l", "listen", {"any": [{"on": {"event": "task.completed"}}]}),
    }
    out = []
    for name, step in places.items():
        kind = next(k for k in step if k != "id")
        step[kind]["due"] = due
        out.append((name, _spec_with_step(step)))
    process_due = copy.deepcopy(PROCESS["spec"])
    process_due["due"] = due
    out.append(("spec", process_due))
    return out


def _spec_with_step(step: dict[str, Any]) -> dict[str, Any]:
    spec = copy.deepcopy(PROCESS["spec"])
    spec["stages"][0]["steps"].insert(0, step)
    return spec


def _catalog_ok(spec: dict[str, Any]) -> bool:
    return CATALOG.is_valid({**PROCESS, "spec": spec})


def _core_shape_errors(spec: dict[str, Any]) -> list[str]:
    return [error.message for error in pd._shape_validator().iter_errors(spec)]


@pytest.mark.parametrize("due", DUE_FORMS, ids=json.dumps)
def test_every_due_form_is_accepted_where_a_deadline_may_stand(due: Any) -> None:
    for place, spec in _with_due(due):
        assert _catalog_ok(spec), place
        assert _core_shape_errors(spec) == [], place


@pytest.mark.parametrize("due", BAD_DUE_FORMS, ids=json.dumps)
def test_a_malformed_due_is_rejected(due: Any) -> None:
    for place, spec in _with_due(due):
        assert not _catalog_ok(spec), place
        assert _core_shape_errors(spec) != [], place


@pytest.mark.parametrize(
    "step",
    [
        {"id": "w", "wait": "PT1H", "due": "P1D"},
        {"id": "w", "wait": {"at": "data.deadline", "due": "P1D"}},
        {"id": "w", "wait": {"workdays": 2}},
        {"id": "w", "wait": {"duration": "PT1H"}},
    ],
    ids=["beside", "inside", "workdays", "duration"],
)
def test_wait_takes_no_deadline(step: dict[str, Any]) -> None:
    spec = _spec_with_step(step)
    assert not _catalog_ok(spec)
    assert _core_shape_errors(spec) != []
    assert _catalog_ok(_spec_with_step({"id": "w", "wait": "PT1H"}))


def test_the_core_check_reports_a_malformed_due_as_a_shape_finding() -> None:
    spec = _spec_with_step(
        _step("h", "human", {"taskType": "review", "assign": [{"role": "lead"}], "due": {}})
    )
    checked = pd.check_process("purchase", pd.normalized_spec(spec), pd.Catalog())
    assert {p.code for p in checked.errors} == {"schema_violation"}


def test_escalations_keep_their_forms() -> None:
    """``after`` of an escalation stays a duration, a CEL moment or ``due``."""
    escalation = CATALOG.schema["$defs"]["escalation"]["properties"]["after"]
    assert escalation == {
        "oneOf": [{"const": "due"}, {"$ref": "#/$defs/durationOrCel"}],
        "description": escalation["description"],
    }


# --- the calendar: working hours --------------------------------------------------------


def test_the_example_calendar_takes_the_working_hours_of_the_adr() -> None:
    hours = {
        "intervals": [{"from": "09:00", "to": "18:00"}],
        "weekdays": {"6": []},
        "shortDayReduction": "PT1H",
    }
    calendar = {**CALENDAR, "spec": {**CALENDAR["spec"], "workingHours": hours}}
    assert CATALOG.is_valid(calendar)
    request = CalendarPublishRequest.model_validate({"key": "ru", "spec": calendar["spec"]})
    assert (
        request.spec.model_dump(mode="json", by_alias=True, exclude_unset=True) == calendar["spec"]
    )


def test_a_weekday_key_read_from_yaml_is_the_weekday() -> None:
    """YAML reads ``weekdays: {6: []}`` with an integer key; the core takes it as ``"6"``."""
    text = "intervals: [{from: '09:00', to: '18:00'}]\nweekdays: {6: []}\n"
    hours = yaml.load(text, Loader=_yaml12_loader())
    assert list(hours["weekdays"]) == [6]
    spec = {**CALENDAR["spec"], "workingHours": hours}
    request = CalendarPublishRequest.model_validate({"key": "ru", "spec": spec})
    dumped = request.spec.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert dumped["workingHours"]["weekdays"] == {"6": []}


def test_a_calendar_without_working_hours_is_sent_as_before() -> None:
    request = CalendarPublishRequest.model_validate({"key": "ru", "spec": CALENDAR["spec"]})
    dumped = request.spec.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert "workingHours" not in dumped
    assert dumped == CALENDAR["spec"]


# --- the package test: expect.sla ------------------------------------------------------


def _test_with_expect(expect: dict[str, Any]) -> dict[str, Any]:
    document = copy.deepcopy(PACKAGE_TEST)
    document["steps"].append({"expect": expect})
    return document


@pytest.mark.parametrize("state", ["ok", "warning", "breached", "paused"])
def test_a_package_test_expects_the_sla_of_a_step_and_of_the_process(state: str) -> None:
    document = _test_with_expect({"sla": {"approve-award": state, "process": state}})
    assert TESTS.is_valid(document)


@pytest.mark.parametrize(
    "sla",
    [{}, {"approve-award": "late"}, {"Approve": "ok"}, {"approve-award": None}, "breached"],
    ids=["empty", "unknown-state", "bad-id", "null", "not-a-map"],
)
def test_a_malformed_sla_expectation_is_rejected(sla: Any) -> None:
    assert not TESTS.is_valid(_test_with_expect({"sla": sla}))


def test_the_core_test_schema_is_the_pinned_one() -> None:
    held = json.loads(TEST_SCHEMA_FILE.read_text("utf-8"))
    assert held == json.loads((PINNED / "test.schema.json").read_text("utf-8"))
    assert "sla" in held["$defs"]["testStep"]["properties"]["expect"]["properties"]


# --- what was valid stays valid ---------------------------------------------------------

EXAMPLES = sorted(
    path
    for pattern in ("*.process.yaml", "*.calendar.yaml", "*.test.yaml")
    for path in FIXTURES.glob(pattern)
)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_the_examples_pass_the_schemas_unchanged(path: Path) -> None:
    document = yaml.load(path.read_text("utf-8"), Loader=_yaml12_loader())
    if path.name.endswith(".test.yaml"):
        assert [e.message for e in TESTS.iter_errors(document)] == []
        return
    assert [e.message for e in CATALOG.iter_errors(document)] == []
    if document["kind"] == "Process":
        assert _core_shape_errors(document["spec"]) == []


def test_the_core_schema_is_rebuilt_from_the_catalog() -> None:
    catalog = json.loads((PINNED / "object.schema.json").read_text("utf-8"))
    held = json.loads(pd.SCHEMA_FILE.read_text("utf-8"))
    assert held == pd.process_schema_from_catalog(catalog)
    assert {"processDue", "workingSpan"} <= set(held["$defs"])
    jsonschema.Draft202012Validator.check_schema(held)
