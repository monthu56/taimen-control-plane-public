"""The revision of the engine's semantics a version runs under (CP-ADR-0074, amendment 2026-09-29).

process-observability P010: a version carries ``engine_revision`` — ``1``
for versions published before the amendment, ``2`` for new ones. The engine
takes it from the version it runs, a replay from the version record, a
candidate replay from the instance's version. Revision 2 adds SLA deadlines
(P011): a journal replays without discrepancies under the revision it was
recorded with, and a process without deadlines behaves alike under either.
"""

from dataclasses import replace
from typing import Any

import pytest

from control_plane.domain import process_definition as pd
from control_plane.domain import process_engine as pe
from control_plane.domain.process_engine import Definition
from control_plane.domain.process_replay import as_version, replay
from tests.unit.test_process_engine import CALENDARS, CATALOG, OTHER, Run, _example, spec
from tests.unit.test_process_replay import TABLE, _instance


def _closed_example() -> Run:
    run = _example()
    run.vote("approve-price", OTHER, "approved", 1)
    run.feed(
        "event",
        {
            "event": run.event_doc(
                "purchase.protocol",
                {
                    "number": "0373100000126000001",
                    "kind": "results",
                    "result": "won",
                    "winner": {"inn": "7711111111", "name": "Us"},
                },
            )
        },
    )
    assert run.status == "completed"
    return run


def test_a_spec_not_published_yet_runs_under_the_latest_revision() -> None:
    body = pd.normalized_spec(spec(TABLE))
    assert pe.ENGINE_REVISIONS == (1, 2)
    assert Definition.build("test", body, CATALOG).engine_revision == pe.ENGINE_REVISION == 2
    assert Definition.build("test", body, CATALOG, engine_revision=1).engine_revision == 1
    with pytest.raises(pe.EngineError, match="unknown engine revision 3"):
        Definition.build("test", body, CATALOG, engine_revision=3)


def _recorded(run: Run, revision: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The journal of ``run``'s inputs as a version of ``revision`` records it, and its state."""
    definition = replace(run.definition, engine_revision=revision)
    state: dict[str, Any] | None = None
    journal = []
    for seq, (given, _, _) in enumerate(run.records):
        state, decisions, intents = pe.step(definition, state, given)
        journal.append(
            {
                "seq": seq,
                "input": given.out(),
                "decisions": [d.out() for d in decisions],
                "intents": [{**i.out(), "executed": {"ok": True}} for i in intents],
                "calendars": {"ru": 1},
            }
        )
    return journal, state


@pytest.mark.parametrize("revision", pe.ENGINE_REVISIONS)
def test_fixture_journals_replay_without_discrepancies_under_their_revision(
    revision: int,
) -> None:
    for run in (_closed_example(), _instance(500), _instance(5000)):
        journal, state = _recorded(run, revision)
        definition = replace(run.definition, engine_revision=revision)
        result = replay(definition, journal, lambda named: CALENDARS)
        assert [d.out() for d in result.discrepancies] == []
        assert result.steps == len(journal) == len(run.records)
        assert result.state == state


def test_journals_without_deadlines_replay_under_either_revision() -> None:
    for run in (_instance(500), _instance(5000)):
        journal, state = _recorded(run, 1)
        result = replay(replace(run.definition, engine_revision=2), journal, lambda n: CALENDARS)
        assert [d.out() for d in result.discrepancies] == []
        assert result.state == state


def test_revision_2_behaves_as_revision_1_step_by_step_without_deadlines() -> None:
    for run in (_instance(500), _instance(5000)):
        old = replace(run.definition, engine_revision=1)
        new = replace(run.definition, engine_revision=2)
        state: dict[str, Any] | None = None
        for given, _, _ in run.records:
            before, old_decisions, old_intents = pe.step(old, state, given)
            after, new_decisions, new_intents = pe.step(new, state, given)
            assert (before, old_decisions, old_intents) == (after, new_decisions, new_intents)
            state = before


def test_a_due_sets_deadline_timers_under_revision_2_only() -> None:
    """A version of revision 1 with ``due`` runs as before: no SLA timers, no facts (FR-031)."""
    run = _closed_example()
    old = replace(run.definition, engine_revision=1)
    state: dict[str, Any] | None = None
    kinds: set[str] = set()
    for given, _, _ in run.records:
        state, _, intents = pe.step(old, state, given)
        kinds |= {i.body["timerKind"] for i in intents if i.kind == "set_timer"}
        assert not [i for i in intents if str(i.body.get("type", "")).startswith("process.sla_")]
    assert kinds and not kinds & {"sla", "sla_warning"}
    # The same inputs under revision 2 (the run's) set the deadline timers.
    recorded = {i.body.get("timerKind") for _, _, made in run.records for i in made}
    assert "sla" in recorded


def test_a_candidate_replays_under_the_revision_of_the_instance_version() -> None:
    candidate = Definition.build("test", pd.normalized_spec({**spec(TABLE), "version": 2}), CATALOG)
    assert candidate.engine_revision == 2
    under = as_version(candidate, 1, 1)
    assert (under.version, under.engine_revision) == (1, 1)
    assert under.programs is candidate.programs
    assert as_version(candidate, 2, 2) is candidate
