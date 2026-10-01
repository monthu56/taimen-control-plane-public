"""Replay of a candidate version on instance journals (CP-ADR-0074 §10; process-packages P014).

SC-005: a version replayed on its own journal gives zero divergences; a
changed row of a decision table shows exactly the instances it affects, at
the journal entry where their paths part.
"""

from typing import Any

from control_plane.domain import process_definition as pd
from control_plane.domain.process_engine import Definition
from control_plane.domain.process_replay import (
    Discrepancy,
    Replay,
    as_version,
    first_divergence,
    replay,
)
from tests.unit.test_process_engine import CALENDARS, CATALOG, Run, spec

TABLE = """
decisions:
  - id: level
    hitPolicy: first
    inputs: [{id: amount, expr: data.amount, type: number}]
    outputs: [{id: level, type: number}]
    rules:
      - {when: {amount: "[0..1000)"}, then: {level: 1}}
      - {when: {amount: "[1000..100000)"}, then: {level: 2}}
stages:
  - id: s
    steps:
      - id: choose
        decide: {table: level}
        output: {as: {level: step.result.level}}
      - id: mark
        set: {note: "'checked'"}
      - id: review
        human: {taskType: review, assign: [{role: lead}]}
        output: {as: {decision: step.result.decision}}
"""


def _journal(run: Run) -> list[dict[str, Any]]:
    """The journal the core writes: inputs whole, decisions, intents with what became of them."""
    return [
        {
            "seq": seq,
            "input": given.out(),
            "decisions": [d.out() for d in decisions],
            "intents": [{**i.out(), "executed": {"ok": True}} for i in intents],
            "calendars": {"ru": 1},
        }
        for seq, (given, decisions, intents) in enumerate(run.records)
    ]


def _instance(amount: int) -> Run:
    run = Run(TABLE)
    run.start(amount=amount)
    run.complete_task("review", {"decision": "go"})
    return run


def _candidate(body: str, version: int = 2) -> Definition:
    return Definition.build("test", pd.normalized_spec({**spec(body), "version": version}), CATALOG)


def _divergence(candidate: Definition, run: Run) -> Any:
    journal = _journal(run)
    result = replay(as_version(candidate, 1, 1), journal, lambda named: CALENDARS, stop=True)
    return first_divergence(result, run.state, len(journal) - 1)


def test_a_version_replayed_on_its_own_journal_has_no_divergence() -> None:
    runs = [_instance(amount) for amount in (100, 500, 5000)]
    candidate = _candidate(TABLE)
    assert [_divergence(candidate, run) for run in runs] == [None, None, None]


def test_the_number_of_the_candidate_is_not_behaviour() -> None:
    run = _instance(100)
    # Under its own number the candidate differs on every process.* event it emits.
    result = replay(_candidate(TABLE), _journal(run), lambda named: CALENDARS, stop=True)
    divergence = first_divergence(result, run.state, 1)
    assert divergence is not None and divergence.kind == "intent"
    assert divergence.recorded["payload"]["version"] == 1
    assert divergence.replayed["payload"]["version"] == 2
    assert _divergence(_candidate(TABLE), run) is None


def test_a_changed_table_row_diverges_exactly_the_instances_it_affects() -> None:
    runs = {amount: _instance(amount) for amount in (100, 500, 700, 5000)}
    changed = TABLE.replace('"[0..1000)"', '"[0..400)"').replace(
        '"[1000..100000)"', '"[400..100000)"'
    )
    candidate = _candidate(changed)
    found = {amount: _divergence(candidate, run) for amount, run in runs.items()}
    assert {amount for amount, d in found.items() if d is not None} == {500, 700}
    divergence = found[500]
    assert divergence.seq == 0  # the start: the table decides there
    assert divergence.kind == "decision" and divergence.element == "choose"
    assert divergence.recorded["rules"] == [0] and divergence.replayed["rules"] == [1]
    assert divergence.out() == {
        "journalSeq": 0,
        "kind": "decision",
        "element": "choose",
        "recorded": divergence.recorded,
        "replayed": divergence.replayed,
    }


def test_a_changed_value_that_decides_nothing_shows_in_the_data() -> None:
    run = _instance(100)
    candidate = _candidate(TABLE.replace("\"'checked'\"", "\"'seen'\""))
    divergence = _divergence(candidate, run)
    # Every step decides and intends the same: only the stored state shows it,
    # at the last entry of the journal.
    assert divergence is not None
    assert (divergence.kind, divergence.element, divergence.seq) == ("data", None, 1)
    assert divergence.recorded["note"] == "checked" and divergence.replayed["note"] == "seen"


def test_a_refused_input_is_the_divergence_of_its_entry() -> None:
    result = Replay(
        steps=2, discrepancies=[Discrepancy(1, "input", {"kind": "task"}, "unknown activity")]
    )
    divergence = first_divergence(result, {"data": {}}, 3)
    assert divergence is not None
    assert (divergence.seq, divergence.kind, divergence.element) == (1, "input", None)
    assert divergence.replayed == "unknown activity"


def test_the_final_state_is_compared_when_every_step_matched() -> None:
    stored = {"data": {"a": 1}, "timers": {}, "seq": 3}
    assert first_divergence(Replay(steps=3, state=dict(stored)), stored, 2) is None
    timers = first_divergence(Replay(steps=3, state={**stored, "timers": {"t": {}}}), stored, 2)
    assert timers is not None and (timers.kind, timers.seq) == ("timer", 2)
    other = first_divergence(Replay(steps=3, state={**stored, "seq": 4}), stored, 2)
    assert other is not None and other.kind == "state"
