"""Addressees of the targets of an escalation level (CP-ADR-0078, amendment 2026-09-30).

The engine names the targets of ``process.escalated`` as text; the core reads
every one back into a candidate and resolves it on its own when it records the
event. A target that does not resolve is ``null`` with its reason, never an
error of the event. The resolution against the database is covered by
``tests/integration/test_process_sla.py``; here — the text and the shape.
"""

import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from control_plane.application.commands.process_instances import _Executor
from control_plane.domain import process_engine as engine
from control_plane.domain.errors import ValidationError

PRINCIPAL = "8d6f1c1e-2b4f-4f4e-9a55-2a4c1f0b7d11"


@pytest.mark.parametrize(
    "candidate",
    [
        {"role": "general-director"},
        {"role": "ns:with:colons"},
        {"agent": "sample-process"},
        {"principal": PRINCIPAL},
    ],
)
def test_the_text_of_a_target_reads_back_into_its_candidate(candidate: dict[str, str]) -> None:
    text = engine._assignee_text(candidate)
    assert engine.assignee_of_text(text) == candidate


@pytest.mark.parametrize(
    ("text", "candidate"),
    [
        ("", {"principal": ""}),
        ("role:", {"role": ""}),
        ("agent:", {"agent": ""}),
        ("Role:boss", {"principal": "Role:boss"}),
    ],
)
def test_a_target_that_names_nothing_is_a_candidate_that_resolves_to_nothing(
    text: str, candidate: dict[str, str]
) -> None:
    assert engine.assignee_of_text(text) == candidate


ROLE = uuid.UUID("0b7e3a52-6f44-4c1a-9f0e-3c8f7d2a1b90")


class _Resolver:
    """What the executor resolves against the database, as a table."""

    workspace = "5e9b7b3c-6a1d-4f7e-8a2b-9c0d1e2f3a4b"

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    async def resolve(
        self, item: Mapping[str, Any], *, field: str
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        assert field == "to"
        self.seen.append(dict(item))
        if item.get("role") == "general-director":
            return None, ROLE
        if item.get("role") is not None:
            raise ValidationError("unknown_role", "no role")
        if item.get("agent") is not None:
            raise ValidationError("unknown_agent", "no agent")
        return uuid.UUID(str(item["principal"])), None

    def address(
        self, *, principal_id: uuid.UUID | None = None, role_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        return {
            "principalId": str(principal_id) if principal_id else None,
            "roleId": str(role_id) if role_id else None,
            "workspaceId": self.workspace,
        }


async def _addressees(targets: Any) -> dict[str, Any]:
    resolver = _Resolver()
    executor: Any = SimpleNamespace(resolve=resolver.resolve, address=resolver.address)
    out: dict[str, Any] = await _Executor.escalation_addressees(executor, targets)
    return out


async def test_one_addressee_per_target_in_its_order_and_the_reasons_of_the_nulls() -> None:
    targets = ["role:nobody", "role:general-director", "agent:gone", "not-a-uuid", PRINCIPAL]
    out = await _addressees(targets)
    workspace = _Resolver.workspace
    assert out["addressees"] == [
        None,
        {"principalId": None, "roleId": str(ROLE), "workspaceId": workspace},
        None,
        None,
        {"principalId": PRINCIPAL, "roleId": None, "workspaceId": workspace},
    ]
    assert out["unresolved"] == [
        {"index": 0, "target": "role:nobody", "reason": "unknown_role"},
        {"index": 2, "target": "agent:gone", "reason": "unknown_agent"},
        {"index": 3, "target": "not-a-uuid", "reason": "unknown_principal"},
    ]


@pytest.mark.parametrize("targets", [None, [], "role:general-director", {"role": "x"}, 7])
async def test_no_list_of_targets_is_no_addressees(targets: Any) -> None:
    assert await _addressees(targets) == {"addressees": [], "unresolved": []}


async def test_a_null_or_empty_target_is_an_unresolved_one() -> None:
    out = await _addressees([None, ""])
    assert out["addressees"] == [None, None]
    assert [(u["target"], u["reason"]) for u in out["unresolved"]] == [
        ("", "unknown_principal"),
        ("", "unknown_principal"),
    ]


async def test_the_same_target_twice_is_resolved_twice_and_alike() -> None:
    out = await _addressees(["role:general-director", "role:general-director"])
    assert out["addressees"][0] == out["addressees"][1] is not None
    assert out["unresolved"] == []


async def test_a_long_target_is_cut_in_the_reason() -> None:
    out = await _addressees(["role:" + "x" * 500])
    assert len(out["unresolved"][0]["target"]) == 200
