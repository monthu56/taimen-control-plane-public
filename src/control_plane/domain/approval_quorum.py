"""Quorum of an ``approve`` step: a pure function (CP-ADR-0074 §7).

One approval of the core is one decision of one approver; a step that needs
several decisions asks for one approval per approver, and this module says
what their votes add up to. No database, no clock: the engine feeds it the
current vote of every approver and executes what it answers.

``parse_quorum``
    The ``quorum`` of a step — ``all``, ``any``, ``{atLeast: n}`` or
    ``{percent: p}`` — as a value object.
``tally``
    The votes so far → the outcome (``pending``, ``approved``, ``rejected``)
    and the approvers whose approval must be open now. The engine requests the
    missing ones and cancels open approvals that are no longer in ``active``:
    after an early decision nobody is active, so the rest are closed.

An approver who left (``approval.cancelled``) is ``withdrawn``: they no longer
count, and the quorum is recomputed over those who remain — ``all`` stops
waiting for them, ``percent`` takes its share of a smaller number, and an
``atLeast`` that the remaining approvers can no longer reach is a rejection.

Separation of duties is deliberately not here: the core refuses the decision
of an excluded principal on every path (``approvals.excluded_principals``).
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal

QuorumKind = Literal["all", "any", "atLeast", "percent"]
Mode = Literal["parallel", "sequential"]
Vote = Literal["pending", "approved", "rejected", "withdrawn"]
Outcome = Literal["pending", "approved", "rejected"]

MODES: tuple[Mode, ...] = ("parallel", "sequential")
VOTES: tuple[Vote, ...] = ("pending", "approved", "rejected", "withdrawn")
MAX_AT_LEAST = 50

#: Why a decided tally is decided.
REASON_REACHED = "quorum_reached"
REASON_UNREACHABLE = "quorum_unreachable"
REASON_NO_APPROVERS = "no_approvers"
REASON_ALL_VOTED = "all_voted"


@dataclass(frozen=True)
class Quorum:
    kind: QuorumKind
    #: ``atLeast`` — a count; ``percent`` — a share in (0, 100]; else ``None``.
    value: Fraction | None = None

    def required(self, eligible: int) -> int:
        """Approvals needed out of ``eligible`` remaining approvers.

        ``all`` and ``percent`` scale with the approvers who remain;
        ``atLeast`` does not — more than remain is simply unreachable.
        """
        if self.kind == "all":
            return eligible
        if self.kind == "any":
            return 1
        assert self.value is not None
        if self.kind == "atLeast":
            return int(self.value)
        return max(1, math.ceil(self.value * eligible / 100))

    def to_json(self) -> Any:
        if self.kind in ("all", "any"):
            return self.kind
        assert self.value is not None
        if self.kind == "atLeast":
            return {"atLeast": int(self.value)}
        number = float(self.value)
        return {"percent": int(number) if number.is_integer() else number}


def parse_quorum(value: Any) -> Quorum:
    """The ``quorum`` of a step as the catalog schema allows it; else ValueError."""
    if value in ("all", "any"):
        return Quorum(value)
    if isinstance(value, Mapping) and len(value) == 1:
        if "atLeast" in value:
            count = value["atLeast"]
            if (
                isinstance(count, int)
                and not isinstance(count, bool)
                and 1 <= count <= MAX_AT_LEAST
            ):
                return Quorum("atLeast", Fraction(count))
        elif "percent" in value:
            share = value["percent"]
            if isinstance(share, int | float) and not isinstance(share, bool):
                # Through the decimal text: 66.7 is 667/10, not the binary float.
                exact = Fraction(str(share))
                if 0 < exact <= 100:
                    return Quorum("percent", exact)
    raise ValueError(
        f"invalid quorum {value!r}: expected all, any, {{atLeast: 1..{MAX_AT_LEAST}}}"
        " or {percent: (0, 100]}"
    )


@dataclass(frozen=True)
class QuorumTally:
    outcome: Outcome
    #: Why the outcome is decided; ``None`` while pending.
    reason: str | None
    approvals: int
    rejections: int
    pending: int
    #: Approvers who have not left.
    eligible: int
    #: Approvals needed out of ``eligible``.
    required: int
    #: Approvers whose approval must be open now, in approver order; empty
    #: once decided.
    active: tuple[str, ...]

    @property
    def decided(self) -> bool:
        return self.outcome != "pending"


def tally(
    approvers: Sequence[str],
    votes: Mapping[str, Vote],
    quorum: Quorum,
    *,
    mode: Mode = "parallel",
    early_decision: bool = True,
) -> QuorumTally:
    """What the votes of ``approvers`` (in their declared order) add up to.

    An approver missing from ``votes`` has not voted. With ``early_decision``
    the step is decided as soon as the quorum is reached or can no longer be
    reached; without it, only when every remaining approver has voted.
    ``sequential`` keeps exactly one approval open — the first approver in
    order who has not voted — ``parallel`` keeps all of them open.
    """
    if mode not in MODES:
        raise ValueError(f"invalid mode {mode!r}: expected one of {', '.join(MODES)}")
    order = list(dict.fromkeys(approvers))
    unknown = sorted(set(votes) - set(order))
    if unknown:
        raise ValueError(f"votes of principals who are not approvers: {', '.join(unknown)}")
    for approver, vote in votes.items():
        if vote not in VOTES:
            raise ValueError(f"invalid vote {vote!r} of {approver}")

    current = [(approver, votes.get(approver, "pending")) for approver in order]
    approvals = sum(1 for _, vote in current if vote == "approved")
    rejections = sum(1 for _, vote in current if vote == "rejected")
    waiting = [approver for approver, vote in current if vote == "pending"]
    eligible = approvals + rejections + len(waiting)
    required = quorum.required(eligible)

    def result(outcome: Outcome, reason: str | None, active: Sequence[str] = ()) -> QuorumTally:
        return QuorumTally(
            outcome=outcome,
            reason=reason,
            approvals=approvals,
            rejections=rejections,
            pending=len(waiting),
            eligible=eligible,
            required=required,
            active=tuple(active),
        )

    if eligible == 0:
        # Everyone left: nobody approved, and an empty "all" is not consent.
        return result("rejected", REASON_NO_APPROVERS)
    if not waiting and not early_decision:
        return result("approved" if approvals >= required else "rejected", REASON_ALL_VOTED)
    if not waiting or early_decision:
        if approvals >= required:
            return result("approved", REASON_REACHED)
        if approvals + len(waiting) < required:
            return result("rejected", REASON_UNREACHABLE)
    return result("pending", None, waiting[:1] if mode == "sequential" else waiting)
