"""Step and SLA events of a process in the catalog (CP-ADR-0074 amendment
2026-09-29 §13, CP-ADR-0078 §3; process-observability P002).

Payloads a writer would produce pass the catalog schema through the same check
the integration package runs on the journal (``tests/event_contract.py``);
payloads that break the contract are refused. The types are events of the
instance, read with ``events.read``, so they carry no instance data.
"""

from typing import Any

import pytest

from control_plane.domain.event_catalog import current_version, get_event_type
from tests.event_contract import payload_violations

INSTANCE = {
    "instanceId": "0b0f6f7e-8f4c-4a54-9a57-6f1f2d6f0a01",
    "definitionKey": "sample.review",
    "version": 2,
    "instanceKey": "case-1",
    "workspaceId": "0b0f6f7e-8f4c-4a54-9a57-6f1f2d6f0a02",
}
ACTIVITY = "0b0f6f7e-8f4c-4a54-9a57-6f1f2d6f0a03"
PRINCIPAL = "0b0f6f7e-8f4c-4a54-9a57-6f1f2d6f0a04"
ROLE = "0b0f6f7e-8f4c-4a54-9a57-6f1f2d6f0a05"
OWNER = {"principalId": None, "roleId": ROLE, "workspaceId": INSTANCE["workspaceId"]}
ASSIGNEE = {"principalId": PRINCIPAL, "roleId": None, "workspaceId": INSTANCE["workspaceId"]}
STEP = {
    "element": "check",
    "stage": "review",
    "stepKind": "human",
    "attempt": 1,
    "activityId": ACTIVITY,
    "enteredAt": "2026-09-29T09:00:00Z",
}
STEP_SLA = {"scope": "step", "element": "check", "attempt": 1, "activityId": ACTIVITY}
PROCESS_SLA = {"scope": "process", "element": None, "attempt": None, "activityId": None}

EXAMPLES: dict[str, dict[str, Any]] = {
    "process.step_entered": {
        **INSTANCE,
        **STEP,
        "waitsFor": "task",
        "taskId": "0b0f6f7e-8f4c-4a54-9a57-6f1f2d6f0a06",
        "approvalIds": [],
        "skillInvocationId": None,
        "childInstanceId": None,
        "due": "2026-10-01T09:00:00Z",
        "warnAt": "2026-10-01T07:00:00Z",
        "provisional": False,
    },
    "process.step_exited": {
        **INSTANCE,
        **STEP,
        "exitedAt": "2026-10-01T10:00:00Z",
        "outcome": "completed",
        "durationSeconds": 176400,
        "due": "2026-10-01T09:00:00Z",
        "breached": True,
        "overdueSeconds": 3600,
    },
    "process.sla_warning": {
        **INSTANCE,
        **STEP_SLA,
        "dueAt": "2026-10-01T09:00:00Z",
        "warnAt": "2026-10-01T07:00:00Z",
        "provisional": False,
        "owner": OWNER,
        "assignee": ASSIGNEE,
    },
    "process.sla_breached": {
        **INSTANCE,
        **STEP_SLA,
        "dueAt": "2026-10-01T09:00:00Z",
        "detectedAt": "2026-10-01T09:00:05Z",
        "overdueSeconds": 5,
        "detectedBy": "timer",
        "provisional": True,
        "owner": OWNER,
        "assignee": ASSIGNEE,
    },
    "process.sla_failed": {
        **INSTANCE,
        **STEP_SLA,
        "error": {"type": "calendar_missing", "status": None, "detail": "calendar ru"},
        "owner": None,
    },
}


def _valid(event_type: str, payload: dict[str, Any]) -> list[str]:
    return payload_violations(event_type, current_version(event_type), payload)


@pytest.mark.parametrize("event_type", sorted(EXAMPLES))
def test_a_new_type_is_an_event_of_the_instance(event_type: str) -> None:
    entry = get_event_type(event_type)
    assert entry.entity_type == "process_instance"
    assert entry.current.version == 1
    required = set(entry.current.schema["required"])
    assert {"instanceId", "definitionKey", "version", "instanceKey"} <= required
    assert "workspaceId" in entry.current.schema["properties"]
    # events.read, not processes.read: no data, memory or output of the instance.
    assert not {"data", "memory", "output", "input"} & set(entry.current.schema["properties"])


@pytest.mark.parametrize("event_type", sorted(EXAMPLES))
def test_a_payload_the_writer_produces_passes_the_catalog(event_type: str) -> None:
    assert _valid(event_type, EXAMPLES[event_type]) == []


@pytest.mark.parametrize("event_type", sorted(EXAMPLES))
def test_every_field_of_the_adr_is_always_present(event_type: str) -> None:
    for name in EXAMPLES[event_type]:
        if name == "workspaceId":
            continue
        payload = {k: v for k, v in EXAMPLES[event_type].items() if k != name}
        assert _valid(event_type, payload), f"{event_type} without {name}"


def test_the_process_deadline_has_no_step() -> None:
    for name in ("process.sla_warning", "process.sla_breached"):
        payload = {**EXAMPLES[name], **PROCESS_SLA, "assignee": None}
        assert _valid(name, payload) == []
    migrated = {**EXAMPLES["process.sla_breached"], **PROCESS_SLA, "detectedBy": "migration"}
    assert _valid("process.sla_breached", migrated) == []


@pytest.mark.parametrize(
    "addressee",
    [
        {"principalId": PRINCIPAL, "roleId": ROLE, "workspaceId": None},
        {"principalId": None, "roleId": None, "workspaceId": None},
        {"principalId": "role:reviewer", "roleId": None, "workspaceId": None},
        {"principalId": PRINCIPAL, "roleId": None},
    ],
    ids=["both", "neither", "slug", "no-workspace"],
)
def test_an_addressee_names_exactly_one_principal_or_role(addressee: dict[str, Any]) -> None:
    for name in ("process.sla_warning", "process.sla_breached"):
        assert _valid(name, {**EXAMPLES[name], "owner": addressee})
        assert _valid(name, {**EXAMPLES[name], "assignee": addressee})
    assert _valid("process.sla_failed", {**EXAMPLES["process.sla_failed"], "owner": addressee})


def test_the_described_values_are_the_adr_ones() -> None:
    def said(event_type: str, field: str) -> str:
        return get_event_type(event_type).current.schema["properties"][field]["description"]

    for kind in ("human", "approve", "call", "recall", "listen", "wait"):
        assert kind in said("process.step_entered", "stepKind")
    for waits in ("task", "approval", "skill", "agent", "child", "event", "time", "memory"):
        assert waits in said("process.step_entered", "waitsFor")
    for outcome in ("completed", "cancelled", "interrupted", "failed", "timed_out", "migrated"):
        assert outcome in said("process.step_exited", "outcome")
    assert "step or process" in said("process.sla_breached", "scope")
    assert "timer or migration" in said("process.sla_breached", "detectedBy")


def test_a_timer_moved_by_a_migration_keeps_the_version_of_its_type() -> None:
    """The amendment adds a value of ``cause``; the schema of the type is the same."""
    entry = get_event_type("process.timer_rescheduled")
    assert entry.current.version == 1
    cause = entry.current.schema["properties"]["cause"]
    assert cause["type"] == "string"
    for value in ("data_changed", "calendar_changed", "resumed", "migrated"):
        assert value in cause["description"]
    payload = {
        **INSTANCE,
        "timerId": "0b0f6f7e-8f4c-4a54-9a57-6f1f2d6f0a07",
        "element": "check",
        "previousDueAt": "2026-10-01T09:00:00Z",
        "dueAt": "2026-10-02T09:00:00Z",
        "provisional": False,
        "cause": "migrated",
        "changedFields": [],
    }
    assert payload_violations("process.timer_rescheduled", 1, payload) == []
