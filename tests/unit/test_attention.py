"""Pure parts of the attention rules (CP-ADR-0071): keys, scores, run streaks."""

import uuid
from datetime import timedelta

from control_plane.application.common import utcnow
from control_plane.application.queries.attention import (
    DUE_SOON,
    RULES,
    AttentionItem,
    approval_score,
    blocked_score,
    due_score,
    failing_score,
    item_actions,
    item_key,
    parse_item_key,
    trailing_failures,
    undecidable_score,
)


def test_item_key_roundtrip_and_malformed_keys() -> None:
    entity = uuid.uuid4()
    assert parse_item_key(item_key("approval.review", entity)) == ("approval.review", entity)
    for bad in ("", "nonsense", ":" + str(entity), "task.blocked:not-a-uuid"):
        assert parse_item_key(bad) is None


def test_rules_are_versioned_and_unique() -> None:
    refs = [rule.ref for rule in RULES]
    assert refs == [
        "approval.review@1",
        "approval.decide@1",
        "approval.undecidable@1",
        "task.due_not_started@1",
        "task.blocked@1",
        "task.delegated_failing@1",
    ]
    assert len({rule.key for rule in RULES}) == len(RULES)


def test_scores_order_the_kinds_and_stay_in_range() -> None:
    assert approval_score(gate=True, waiting=timedelta(0), priority=None) == 80
    assert approval_score(gate=False, waiting=timedelta(0), priority=None) == 70
    assert approval_score(gate=False, waiting=timedelta(days=30), priority="critical") == 90
    assert undecidable_score(waiting=timedelta(0)) == 75
    assert undecidable_score(waiting=timedelta(days=30)) == 85
    assert due_score(left=timedelta(hours=-1), priority="medium") == 90
    assert due_score(left=DUE_SOON, priority="medium") == 60
    assert due_score(left=timedelta(0) + DUE_SOON / 2, priority="medium") == 70
    assert due_score(left=timedelta(hours=-1), priority="critical") == 100
    assert blocked_score(priority="low") == 50
    assert failing_score(failures=3, priority="medium") == 65
    assert failing_score(failures=10, priority="critical") == 90


def test_trailing_failures_count_the_streak_only() -> None:
    assert trailing_failures([]) == 0
    assert trailing_failures(["failed", "failed", "failed"]) == 3
    assert trailing_failures(["failed", "succeeded", "failed", "failed"]) == 2
    assert trailing_failures(["failed", "cancelled", "failed"]) == 1
    # A running attempt neither counts nor breaks the streak.
    assert trailing_failures(["failed", "failed", "failed", "running"]) == 3


def test_actions_are_calls_of_the_api() -> None:
    entity = uuid.uuid4()
    item = AttentionItem(
        rule_key="task.delegated_failing",
        rule_version=1,
        kind="delegated_failure",
        reason_code="runs_failed",
        entity_type="task",
        entity_id=entity,
        score=65,
        title="t",
        workspace_id=None,
        since=utcnow(),
    )
    assert item_actions(item) == [
        {"action": "open", "method": "GET", "href": f"/api/v1/tasks/{entity}"},
        {"action": "listRuns", "method": "GET", "href": f"/api/v1/runs?taskId={entity}"},
        {"action": "update", "method": "PATCH", "href": f"/api/v1/tasks/{entity}"},
        {
            "action": "feedback",
            "method": "POST",
            "href": f"/api/v1/me/attention/task.delegated_failing:{entity}:feedback",
        },
    ]


def test_an_undecidable_approval_offers_no_decision() -> None:
    """Its addressee may not decide it: open the approval and the process, nothing else."""
    entity, instance = uuid.uuid4(), uuid.uuid4()

    def item(process: uuid.UUID | None) -> AttentionItem:
        return AttentionItem(
            rule_key="approval.undecidable",
            rule_version=1,
            kind="undecidable",
            reason_code="undecidable_process_owner",
            entity_type="approval",
            entity_id=entity,
            score=75,
            title="t",
            workspace_id=None,
            since=utcnow(),
            details={"processInstanceId": str(process) if process else None},
        )

    feedback = {
        "action": "feedback",
        "method": "POST",
        "href": f"/api/v1/me/attention/approval.undecidable:{entity}:feedback",
    }
    opened = {"action": "open", "method": "GET", "href": f"/api/v1/approvals/{entity}"}
    assert item_actions(item(instance)) == [
        opened,
        {"action": "openProcess", "method": "GET", "href": f"/api/v1/process-instances/{instance}"},
        feedback,
    ]
    assert item_actions(item(None)) == [opened, feedback]
