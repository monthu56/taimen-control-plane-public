"""Quorum of an ``approve`` step (CP-ADR-0074 §7, process-packages P008)."""

from fractions import Fraction
from typing import Any

import pytest

from control_plane.domain.approval_quorum import (
    REASON_ALL_VOTED,
    REASON_NO_APPROVERS,
    REASON_REACHED,
    REASON_UNREACHABLE,
    Quorum,
    Vote,
    parse_quorum,
    tally,
)

ABC = ["a", "b", "c"]
TWO_OF_THREE = parse_quorum({"atLeast": 2})


# --- parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("all", Quorum("all")),
        ("any", Quorum("any")),
        ({"atLeast": 2}, Quorum("atLeast", Fraction(2))),
        ({"percent": 50}, Quorum("percent", Fraction(50))),
        ({"percent": 66.7}, Quorum("percent", Fraction(667, 10))),
        ({"percent": 100}, Quorum("percent", Fraction(100))),
    ],
)
def test_a_quorum_the_catalog_allows_is_parsed(value: Any, expected: Quorum) -> None:
    quorum = parse_quorum(value)
    assert quorum == expected
    assert quorum.to_json() == value


@pytest.mark.parametrize(
    "value",
    [
        "most",
        None,
        {"atLeast": 0},
        {"atLeast": 51},
        {"atLeast": 1.5},
        {"atLeast": True},
        {"percent": 0},
        {"percent": 101},
        {"percent": "50"},
        {"atLeast": 1, "percent": 50},
        {},
    ],
)
def test_anything_else_is_refused(value: Any) -> None:
    with pytest.raises(ValueError, match="invalid quorum"):
        parse_quorum(value)


def test_required_approvals_per_quorum() -> None:
    assert parse_quorum("all").required(3) == 3
    assert parse_quorum("any").required(3) == 1
    assert TWO_OF_THREE.required(3) == 2
    # atLeast does not shrink with the approvers: more than remain is unreachable.
    assert TWO_OF_THREE.required(1) == 2
    assert parse_quorum({"percent": 50}).required(3) == 2
    assert parse_quorum({"percent": 50}).required(4) == 2
    assert parse_quorum({"percent": 66.7}).required(3) == 3  # 2.001 -> 3
    assert parse_quorum({"percent": 66.6}).required(3) == 2  # 1.998 -> 2
    assert parse_quorum({"percent": 1}).required(3) == 1


# --- two of three -----------------------------------------------------------------


def test_two_of_three_approve_early() -> None:
    first = tally(ABC, {"a": "approved"}, TWO_OF_THREE)
    assert first.outcome == "pending"
    assert first.active == ("b", "c")

    decided = tally(ABC, {"a": "approved", "b": "approved"}, TWO_OF_THREE)
    assert decided.outcome == "approved"
    assert decided.reason == REASON_REACHED
    assert (decided.approvals, decided.rejections, decided.pending) == (2, 0, 1)
    # The third approver's approval is no longer needed: nobody stays active,
    # so the engine closes it.
    assert decided.active == ()


def test_two_of_three_reject_early() -> None:
    first = tally(ABC, {"b": "rejected"}, TWO_OF_THREE)
    assert first.outcome == "pending"

    decided = tally(ABC, {"b": "rejected", "c": "rejected"}, TWO_OF_THREE)
    assert decided.outcome == "rejected"
    assert decided.reason == REASON_UNREACHABLE
    assert decided.active == ()


def test_two_of_three_with_one_of_each_waits_for_the_third() -> None:
    split = tally(ABC, {"a": "approved", "b": "rejected"}, TWO_OF_THREE)
    assert split.outcome == "pending"
    assert split.active == ("c",)
    for third in ("approved", "rejected"):
        votes: dict[str, Vote] = {"a": "approved", "b": "rejected", "c": third}
        assert tally(ABC, votes, TWO_OF_THREE).outcome == third


def test_without_early_decision_everyone_votes_first() -> None:
    votes: dict[str, Vote] = {"a": "approved", "b": "approved"}
    waiting = tally(ABC, votes, TWO_OF_THREE, early_decision=False)
    assert waiting.outcome == "pending"
    assert waiting.active == ("c",)

    done = tally(ABC, {**votes, "c": "rejected"}, TWO_OF_THREE, early_decision=False)
    assert done.outcome == "approved"
    assert done.reason == REASON_ALL_VOTED

    lost = tally(
        ABC, {"a": "rejected", "b": "rejected", "c": "approved"}, TWO_OF_THREE, early_decision=False
    )
    assert lost.outcome == "rejected"
    assert lost.reason == REASON_ALL_VOTED


# --- all, any, percent -------------------------------------------------------------


def test_all_is_rejected_by_one_and_approved_by_everyone() -> None:
    everyone = parse_quorum("all")
    assert tally(ABC, {"a": "approved", "b": "approved"}, everyone).outcome == "pending"
    assert tally(ABC, {"b": "rejected"}, everyone).outcome == "rejected"
    approved = tally(ABC, dict.fromkeys(ABC, "approved"), everyone)
    assert approved.outcome == "approved"


def test_any_is_approved_by_one_and_rejected_by_everyone() -> None:
    anyone = parse_quorum("any")
    assert tally(ABC, {"c": "approved"}, anyone).outcome == "approved"
    assert tally(ABC, {"a": "rejected", "b": "rejected"}, anyone).outcome == "pending"
    assert tally(ABC, dict.fromkeys(ABC, "rejected"), anyone).outcome == "rejected"


def test_percent_of_four() -> None:
    half = parse_quorum({"percent": 50})
    four = ["a", "b", "c", "d"]
    assert tally(four, {"a": "approved"}, half).outcome == "pending"
    assert tally(four, {"a": "approved", "d": "approved"}, half).outcome == "approved"
    rejected = tally(four, {"a": "rejected", "b": "rejected", "c": "rejected"}, half)
    assert rejected.outcome == "rejected"


# --- parallel and sequential -----------------------------------------------------


def test_parallel_keeps_every_waiting_approver_open() -> None:
    assert tally(ABC, {}, TWO_OF_THREE).active == ("a", "b", "c")
    assert tally(ABC, {"b": "rejected"}, TWO_OF_THREE, mode="parallel").active == ("a", "c")


def test_sequential_asks_one_approver_at_a_time_in_order() -> None:
    def active(votes: dict[str, Vote]) -> tuple[str, ...]:
        return tally(ABC, votes, TWO_OF_THREE, mode="sequential").active

    assert active({}) == ("a",)
    assert active({"a": "approved"}) == ("b",)
    assert active({"a": "rejected"}) == ("b",)
    assert active({"a": "rejected", "b": "approved"}) == ("c",)
    # Two approvals: decided before the third approver is ever asked.
    decided = tally(ABC, {"a": "approved", "b": "approved"}, TWO_OF_THREE, mode="sequential")
    assert decided.outcome == "approved"
    assert decided.active == ()


def test_sequential_all_stops_at_the_first_rejection() -> None:
    decided = tally(ABC, {"a": "approved", "b": "rejected"}, parse_quorum("all"), mode="sequential")
    assert decided.outcome == "rejected"
    assert decided.active == ()


# --- an approver leaves ------------------------------------------------------------


def test_all_stops_waiting_for_an_approver_who_left() -> None:
    everyone = parse_quorum("all")
    votes: dict[str, Vote] = {"a": "approved", "b": "approved"}
    assert tally(ABC, votes, everyone).outcome == "pending"
    recomputed = tally(ABC, {**votes, "c": "withdrawn"}, everyone)
    assert recomputed.outcome == "approved"
    assert (recomputed.eligible, recomputed.required) == (2, 2)


def test_percent_takes_its_share_of_those_who_remain() -> None:
    most = parse_quorum({"percent": 75})
    four = ["a", "b", "c", "d"]
    votes: dict[str, Vote] = {"a": "approved", "b": "approved"}
    assert tally(four, votes, most).required == 3
    recomputed = tally(four, {**votes, "d": "withdrawn"}, most)
    assert recomputed.required == 3  # ceil(2.25)
    assert recomputed.outcome == "pending"
    assert tally(four, {**votes, "c": "withdrawn", "d": "withdrawn"}, most).outcome == "approved"


def test_at_least_out_of_reach_after_a_departure_is_a_rejection() -> None:
    votes: dict[str, Vote] = {"a": "approved", "b": "withdrawn"}
    assert tally(ABC, votes, TWO_OF_THREE).outcome == "pending"
    decided = tally(ABC, {**votes, "c": "withdrawn"}, TWO_OF_THREE)
    assert decided.outcome == "rejected"
    assert decided.reason == REASON_UNREACHABLE


def test_a_departure_moves_sequential_to_the_next_approver() -> None:
    votes: dict[str, Vote] = {"a": "withdrawn"}
    assert tally(ABC, votes, TWO_OF_THREE, mode="sequential").active == ("b",)


def test_when_everyone_left_nobody_consented() -> None:
    decided = tally(ABC, dict.fromkeys(ABC, "withdrawn"), parse_quorum("all"))
    assert decided.outcome == "rejected"
    assert decided.reason == REASON_NO_APPROVERS
    assert tally([], {}, parse_quorum("any")).outcome == "rejected"


# --- inputs ----------------------------------------------------------------------


def test_a_repeated_approver_counts_once() -> None:
    result = tally(["a", "a", "b"], {"a": "approved"}, parse_quorum("all"))
    assert (result.eligible, result.active) == (2, ("b",))


def test_a_vote_of_a_stranger_or_an_unknown_vote_is_refused() -> None:
    with pytest.raises(ValueError, match="not approvers"):
        tally(ABC, {"z": "approved"}, TWO_OF_THREE)
    with pytest.raises(ValueError, match="invalid vote"):
        tally(ABC, {"a": "maybe"}, TWO_OF_THREE)  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="invalid mode"):
        tally(ABC, {}, TWO_OF_THREE, mode="random")  # type: ignore[arg-type]
