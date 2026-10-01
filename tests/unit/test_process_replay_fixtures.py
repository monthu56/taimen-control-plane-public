"""The replay gate on journals recorded by ``main`` (process-observability P018).

``tests/fixtures/process_journals`` holds what a core without the feature
stored of its instances: versions of engine revision 1 with ``due`` and
escalations, a suspension, a new calendar version, a migration by a package
apply (README.md there names the commit that recorded them). The feature's
engine replays every one of them under revision 1 with no divergence — not
in the decisions, not in the intents, not in the final state
(:func:`process_replay.first_divergence`, as ``POST
/process-definitions/{key}:replay`` compares): FR-028, FR-031, SC-009.
"""

import copy
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from control_plane.domain import process_engine as engine
from control_plane.domain import process_replay
from control_plane.domain.calendar import Calendar
from control_plane.domain.process_definition import Catalog
from control_plane.domain.process_migration import migrate_state, recount_body

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "process_journals"
FILES = sorted(FIXTURES.glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return fixture


def _catalog(fixture: Mapping[str, Any]) -> Catalog:
    return Catalog(
        task_types={t["key"]: t["field_schema"] or None for t in fixture["catalog"]["taskTypes"]},
        agents=frozenset(fixture["catalog"]["agents"]),
        calendars=frozenset(c["key"] for c in fixture["calendars"]),
        versions={d["version"]: d["spec"] for d in fixture["definitions"]},
    )


def _calendars(fixture: Mapping[str, Any]) -> Callable[[Mapping[str, int]], dict[str, Calendar]]:
    versions = {
        (c["key"], c["version"]): Calendar.from_spec(c["spec"]) for c in fixture["calendars"]
    }

    def calendars(named: Mapping[str, int]) -> dict[str, Calendar]:
        return {k: versions[(k, int(v))] for k, v in named.items() if (k, int(v)) in versions}

    return calendars


def _definition(
    fixture: Mapping[str, Any], version: int, *, spec: Mapping[str, Any] | None = None
) -> engine.Definition:
    """The version as the feature's core builds it: the revision its row has, 1 before it."""
    [row] = [d for d in fixture["definitions"] if d["version"] == version]
    return engine.Definition.build(
        fixture["process"],
        spec if spec is not None else row["spec"],
        _catalog(fixture),
        engine_revision=int(row.get("engine_revision") or 1),
    )


def _instances() -> list[Any]:
    return [
        pytest.param(
            path.stem, instance["instance_key"], id=f"{path.stem}:{instance['instance_key']}"
        )
        for path in FILES
        for instance in _load(path)["instances"]
    ]


def _instance(name: str, instance_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = _load(FIXTURES / f"{name}.json")
    [instance] = [i for i in fixture["instances"] if i["instance_key"] == instance_key]
    return fixture, instance


def _replay(
    fixture: Mapping[str, Any], instance: Mapping[str, Any], journal: list[dict[str, Any]]
) -> process_replay.Divergence | None:
    definition = _definition(fixture, instance["definition_version"])
    result = process_replay.replay(definition, journal, _calendars(fixture), stop=True)
    return process_replay.first_divergence(result, instance["state"], int(journal[-1]["seq"]))


# --- the gate ------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "instance_key"), _instances())
def test_every_recorded_instance_replays_with_no_divergence(name: str, instance_key: str) -> None:
    fixture, instance = _instance(name, instance_key)
    divergence = _replay(fixture, instance, instance["journal"])
    assert divergence is None, json.dumps(divergence.out(), default=str, indent=1)


def test_a_decision_other_than_recorded_is_a_divergence() -> None:
    """The gate can fail: a journal whose decision the engine does not make diverges there."""
    fixture, instance = _instance("due-and-escalations", "D-overdue")
    journal = copy.deepcopy(instance["journal"])
    [fired] = [e for e in journal if e["input"]["kind"] == "timer"][:1]
    escalated = next(d for d in fired["decisions"] if d["kind"] == "escalated")
    escalated["level"] = 7
    divergence = _replay(fixture, instance, journal)
    assert divergence is not None
    assert (divergence.seq, divergence.kind, divergence.element) == (
        fired["seq"],
        "decision",
        "sign",
    )


def test_a_state_other_than_stored_is_a_divergence() -> None:
    """Every step matched, the stored state did not: the timers are named."""
    fixture, instance = _instance("due-and-escalations", "D-held")
    stored = copy.deepcopy(instance)
    for timer in stored["state"]["timers"].values():
        timer["frozenAt"] = timer["frozenFrom"]
    divergence = _replay(fixture, stored, instance["journal"])
    assert divergence is not None
    assert divergence.kind == "timer"


# --- the fixtures are what the gate needs --------------------------------------------------


def test_the_fixtures_were_recorded_by_main_before_the_feature() -> None:
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    [commit] = set(re.findall(r"`([0-9a-f]{40})`", readme))
    assert FILES
    for path in FILES:
        fixture = _load(path)
        assert fixture["recordedWith"] == {"ref": "main", "commit": commit}, path.name
        # The schema before the feature has no engine revision on a version.
        assert all("engine_revision" not in d for d in fixture["definitions"]), path.name


def test_the_fixtures_cover_due_escalations_a_suspension_and_a_migration() -> None:
    decisions: Counter[str] = Counter()
    inputs: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    dues: set[str] = set()
    for path in FILES:
        fixture = _load(path)
        for definition in fixture["definitions"]:
            text = json.dumps(definition["spec"])
            dues |= {
                form for form in ('"due": "P', '"due": {"at"', "cal.addWorkdays") if form in text
            }
        for instance in fixture["instances"]:
            statuses[instance["status"]] += 1
            for entry in instance["journal"]:
                inputs[entry["input"]["kind"]] += 1
                decisions.update(d["kind"] for d in entry["decisions"])
    assert dues == {'"due": "P', '"due": {"at"', "cal.addWorkdays"}
    for kind in ("escalated", "timer_fired", "timer_rescheduled", "error_caught"):
        assert decisions[kind], kind
    for kind in ("suspended", "resumed", "migrated"):
        assert decisions[kind], kind
    for kind in ("start", "task", "event", "timer", "command", "calendar", "migrate"):
        assert inputs[kind], kind
    assert set(statuses) == {"completed", "running", "suspended", "cancelled"}


# --- revision 1 keeps the state it had -----------------------------------------------------


def test_a_suspension_under_revision_1_keeps_the_timers_as_before() -> None:
    """``frozenAt`` and ``remainingUnit`` are revision 2's: revision 1 writes neither."""
    fixture, instance = _instance("migration", "M-open")
    definition = _definition(fixture, 2)
    assert definition.engine_revision == 1
    at = datetime.fromisoformat(instance["journal"][-1]["at"].replace("Z", "+00:00"))
    suspend = engine.Input("command", at + timedelta(hours=1), {"action": "suspend"}, None, {})
    state, _, _ = engine.step(definition, copy.deepcopy(instance["state"]), suspend)
    [timer] = state["timers"].values()
    assert timer["state"] == "frozen"
    assert "frozenAt" not in timer and "remainingUnit" not in timer
    resume = engine.Input("command", at + timedelta(hours=2), {"action": "resume"}, None, {})
    state, _, _ = engine.step(definition, state, resume)
    [timer] = state["timers"].values()
    assert timer["state"] == "pending"
    assert "frozenAt" not in timer and "remainingUnit" not in timer


def test_a_suspended_instance_of_revision_1_migrates_onto_revision_2() -> None:
    """A timer frozen before the feature has no ``frozenAt``: its moment is counted back.

    ``frozenFrom`` less what it keeps is when it was frozen; the new deadline's
    remainder is measured from there, not from the migration.
    """
    fixture, instance = _instance("migration", "M-open")
    old = _definition(fixture, 2)
    at = datetime.fromisoformat(instance["journal"][-1]["at"].replace("Z", "+00:00"))
    frozen = at + timedelta(hours=1)
    suspend = engine.Input("command", frozen, {"action": "suspend"}, None, {})
    state, _, _ = engine.step(old, copy.deepcopy(instance["state"]), suspend)
    [kept] = state["timers"].values()
    assert kept["remaining"] is not None

    spec: dict[str, Any] = copy.deepcopy(dict(_definition(fixture, 2).spec))
    spec["version"] = 3
    spec["stages"][0]["steps"][0]["human"]["due"] = "P2D"
    spec["migrations"] = [{"from": 2, "to": 3, "policy": "migrate"}]
    new = engine.Definition.build(
        fixture["process"], spec, _catalog(fixture), engine_revision=engine.SLA_REVISION
    )
    moved = migrate_state(old, new, state, {})
    moved["seq"] = int(state["seq"]) + 1
    later = frozen + timedelta(hours=5)
    recount = engine.Input(
        "migrated",
        later,
        recount_body(from_version=2, target=new),
        None,
        _calendars(fixture)({"ru": 1}),
    )
    state, _, _ = engine.step(new, moved, recount)
    escalation = state["timers"][kept["id"]]
    assert escalation["state"] == "frozen"
    [activity] = state["activities"].values()
    due = datetime.fromisoformat(activity["openedAt"].replace("Z", "+00:00")) + timedelta(days=2)
    assert escalation["remaining"] == pytest.approx((due - frozen).total_seconds(), abs=1)
    assert datetime.fromisoformat(escalation["frozenFrom"].replace("Z", "+00:00")).astimezone(
        UTC
    ) == due.astimezone(UTC)


def test_an_instance_main_suspended_migrates_onto_revision_2() -> None:
    """The state ``main`` stored of a suspended instance: its frozen timers have no ``frozenAt``."""
    fixture, instance = _instance("due-and-escalations", "D-held")
    assert all("frozenAt" not in t for t in instance["state"]["timers"].values())
    old = _definition(fixture, 1)
    spec: dict[str, Any] = copy.deepcopy(dict(old.spec))
    spec["version"] = 2
    spec["stages"][0]["steps"][1]["try"]["do"][0]["human"]["due"] = {"workdays": 1}
    spec["migrations"] = [{"from": 1, "to": 2, "policy": "migrate"}]
    new = engine.Definition.build(
        fixture["process"], spec, _catalog(fixture), engine_revision=engine.SLA_REVISION
    )
    moved = migrate_state(old, new, instance["state"], {})
    moved["seq"] = int(instance["state"]["seq"]) + 1
    at = datetime.fromisoformat(instance["journal"][-1]["at"].replace("Z", "+00:00"))
    recount = engine.Input(
        "migrated",
        at + timedelta(hours=1),
        recount_body(from_version=1, target=new),
        None,
        _calendars(fixture)({"ru": 2}),
    )
    state, decisions, _ = engine.step(new, moved, recount)
    assert state["status"] == "suspended"
    assert {t["state"] for t in state["timers"].values()} == {"frozen"}
    assert "deadline_migrated" in [d.kind for d in decisions]
