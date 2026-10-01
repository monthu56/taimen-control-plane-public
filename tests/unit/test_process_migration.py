"""Moving an open instance to another version by a migration map (CP-ADR-0074 §11; P015).

SC-006: a step renamed with a map while instances wait on it — every instance
carries on on the new version from the same place: its task, its timer, its
position in the stage.
"""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from control_plane.domain import process_definition as pd
from control_plane.domain import process_engine as pe
from control_plane.domain.process_engine import Definition, Input
from control_plane.domain.process_migration import (
    MIGRATION_INPUT,
    MigrationError,
    migrate_state,
    migration_for,
    migration_record,
    standing,
    uncovered,
)
from control_plane.domain.process_replay import replay
from tests.unit.test_process_engine import CALENDARS, CATALOG, Run, spec

V1 = """
stages:
  - id: prepare
    steps:
      - id: draft
        set: {note: "'draft'"}
      - id: review
        human:
          taskType: review
          assign: [{role: lead}]
          due: {at: "cal.addWorkdays(data.deadline, -3)"}
          escalations: [{after: due, action: notify, to: [{role: boss}]}]
        output: {as: {decision: step.result.decision}}
      - id: close
        set: {outcome: "'done'"}
"""

# review is renamed check, a step comes before it, a stage after the first.
V2 = """
migrations:
  - {from: 1, to: 2, policy: migrate, map: {review: check}}
stages:
  - id: prepare
    steps:
      - id: draft
        set: {note: "'draft'"}
      - id: intro
        set: {value: "'intro'"}
      - id: check
        human:
          taskType: review
          assign: [{role: lead}]
          due: {at: "cal.addWorkdays(data.deadline, -3)"}
          escalations: [{after: due, action: notify, to: [{role: boss}]}]
        output: {as: {decision: step.result.decision}}
      - id: close
        set: {outcome: "'done'"}
  - id: archive
    entry: stage.prepare.completed
    steps:
      - id: store
        set: {trail: "['archived']"}
"""


def _definition(body: str, version: int) -> Definition:
    return Definition.build("test", pd.normalized_spec({**spec(body), "version": version}), CATALOG)


def _waiting() -> Run:
    """An instance of version 1 waiting on the task of ``review``."""
    run = Run(V1)
    run.start()
    run.activity("review")
    return run


def test_an_instance_stands_on_its_stage_its_waiting_step_and_its_timer() -> None:
    run = _waiting()
    assert run.state is not None
    assert standing(run.definition, run.state) == {"prepare", "review"}
    v2 = _definition(V2, 2)
    assert uncovered(run.definition, run.state, v2) == ["review"]
    assert uncovered(run.definition, run.state, v2, {"review": "check"}) == []


def test_a_renamed_step_carries_the_instance_on_from_the_same_place() -> None:
    run = _waiting()
    assert run.state is not None
    v2 = _definition(V2, 2)
    moved = migrate_state(run.definition, v2, run.state, {"review": "check"})

    assert moved["version"] == 2
    activity = next(iter(moved["activities"].values()))
    assert activity["element"] == "check"
    [thread] = moved["threads"].values()
    [frame] = thread["stack"]
    assert frame["block"] == "stage:prepare/steps"
    assert frame["index"] == 3  # after check: draft, intro, check
    timers = {t["kind"]: t for t in moved["timers"].values()}
    assert sorted(timers) == ["escalation", "sla"]
    timer = timers["escalation"]
    assert timer["element"] == "check"
    # The escalation after the due: its expression moved with the step.
    assert timer["recipe"]["due"]["path"] == "/spec/stages/0/steps/2/human/due/at"
    assert timer["recipe"]["due"]["path"] in v2.programs
    # So did the deadline of the step (revision 2): the same due.
    assert timers["sla"]["element"] == "check"
    assert timers["sla"]["recipe"] == timer["recipe"]["due"]
    assert moved["stages"]["archive"]["state"] == "available"
    # The state the engine keeps is JSON: the migrated one too.
    assert json.loads(json.dumps(moved)) == moved

    # The next input is an ordinary step of version 2: the task answers check.
    task = {"id": "task-1", "status": "done", "customFields": {"decision": "go"}}
    given = Input(
        "task",
        run.clock + timedelta(hours=1),
        {"activityId": activity["id"], "status": "completed", "task": task},
        None,
        CALENDARS,
    )
    after, decisions, _ = pe.step(v2, moved, given)
    assert after["data"]["decision"] == "go"
    assert after["data"]["outcome"] == "done"
    assert "value" not in after["data"]  # intro stands before check: it never runs
    assert after["data"]["trail"] == ["archived"]
    assert after["status"] == pe.COMPLETED
    assert any(d.kind == "step_completed" and d.element == "check" for d in decisions)


def test_an_element_gone_without_a_map_is_refused_by_name() -> None:
    run = _waiting()
    assert run.state is not None
    with pytest.raises(MigrationError) as refused:
        migrate_state(run.definition, _definition(V2, 2), run.state, {})
    assert refused.value.code == "migration_required"
    assert refused.value.elements == ("review",)


def test_an_element_of_another_kind_cannot_carry_the_instance() -> None:
    run = _waiting()
    assert run.state is not None
    other = V1.replace(
        """      - id: review
        human:
          taskType: review
          assign: [{role: lead}]
          due: {at: "cal.addWorkdays(data.deadline, -3)"}
          escalations: [{after: due, action: notify, to: [{role: boss}]}]
        output: {as: {decision: step.result.decision}}
""",
        """      - id: review
        set: {decision: "'auto'"}
""",
    )
    v2 = _definition(other, 2)
    assert uncovered(run.definition, run.state, v2) == ["review"]
    with pytest.raises(MigrationError):
        migrate_state(run.definition, v2, run.state, None)


def test_a_moved_step_outside_its_block_is_refused() -> None:
    run = _waiting()
    assert run.state is not None
    # check moved into another stage: the thread of prepare has nowhere to stand.
    moved = V2.replace(
        """      - id: check
        human:
          taskType: review
          assign: [{role: lead}]
          due: {at: "cal.addWorkdays(data.deadline, -3)"}
          escalations: [{after: due, action: notify, to: [{role: boss}]}]
        output: {as: {decision: step.result.decision}}
""",
        "",
    ).replace(
        """      - id: store
""",
        """      - id: check
        human:
          taskType: review
          assign: [{role: lead}]
          due: {at: "cal.addWorkdays(data.deadline, -3)"}
          escalations: [{after: due, action: notify, to: [{role: boss}]}]
        output: {as: {decision: step.result.decision}}
      - id: store
""",
    )
    with pytest.raises(MigrationError) as refused:
        migrate_state(run.definition, _definition(moved, 2), run.state, {"review": "check"})
    assert refused.value.code == "element_moved"
    assert refused.value.elements == ("review",)


def test_the_migration_of_a_version_is_the_one_into_the_version_published() -> None:
    body = pd.normalized_spec({**spec(V2), "version": 2})
    assert migration_for(body, 1) == (0, body["migrations"][0])
    assert migration_for(body, 3) is None
    assert migration_for({**body, "version": 3}, 1) is None  # an older version's migration


def test_a_replay_starts_from_the_last_migration_of_the_journal() -> None:
    run = _waiting()
    assert run.state is not None
    v2 = _definition(V2, 2)
    moved = migrate_state(run.definition, v2, run.state, {"review": "check"})
    moved["seq"] = run.state["seq"] + 1
    journal: list[dict[str, Any]] = [
        {
            "seq": seq,
            "input": given.out(),
            "decisions": [d.out() for d in decisions],
            "intents": [i.out() for i in intents],
            "calendars": {},
        }
        for seq, (given, decisions, intents) in enumerate(run.records)
    ]
    migration = Input(MIGRATION_INPUT, run.clock, {"state": moved}, None)
    journal.append(
        {
            "seq": moved["seq"],
            "input": migration.out(),
            "decisions": [{"kind": "migrated", "element": None}],
            "intents": [],
            "calendars": {},
        }
    )
    activity = next(iter(moved["activities"].values()))
    task = {"id": "task-1", "status": "done", "customFields": {"decision": "go"}}
    given = Input(
        "task",
        run.clock + timedelta(hours=1),
        {"activityId": activity["id"], "status": "completed", "task": task},
        None,
        CALENDARS,
    )
    after, decisions, intents = pe.step(v2, moved, given)
    journal.append(
        {
            "seq": after["seq"],
            "input": given.out(),
            "decisions": [d.out() for d in decisions],
            "intents": [i.out() for i in intents],
            "calendars": {"ru": 1},
        }
    )
    result = replay(v2, journal, lambda named: CALENDARS)
    assert result.discrepancies == []
    assert result.steps == 1  # only what ran on version 2
    assert result.state == json.loads(json.dumps(after))


def test_the_migration_record_carries_the_engine_revision_of_the_target_version() -> None:
    run = _waiting()
    assert run.state is not None
    old = replace(run.definition, engine_revision=1)  # a version published before revisions
    v2 = _definition(V2, 2)
    assert v2.engine_revision == pe.ENGINE_REVISION == 2
    moved = migrate_state(old, v2, run.state, {"review": "check"})
    body, decision = migration_record(
        from_key="test",
        from_version=1,
        target=v2,
        mapping={"review": "check"},
        plan_hash="sha256:plan",
        state=moved,
    )
    assert body["engineRevision"] == decision["engineRevision"] == 2
    assert body["toVersion"] == decision["toVersion"] == 2
    assert decision == {
        "kind": "migrated",
        "element": None,
        "fromKey": "test",
        "fromVersion": 1,
        "toVersion": 2,
        "engineRevision": 2,
        "policy": "migrate",
        "map": {"review": "check"},
    }
    assert body["state"] == moved and body["planHash"] == "sha256:plan"
