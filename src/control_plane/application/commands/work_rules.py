"""Work rule commands: create, change, enable, disable, archive (CP-ADR-0063).

A rule is tenant data that files work on its own. Writing one needs
``rules.write`` on the rule's workspace (the tenant for a tenant-level rule);
what the rule then does, it does with the authority of the credential that
last enabled it — never with more than that principal could do by hand.

Every document is validated by ``domain/work_rules.py`` on write; the names a
rule refers to (a task type key, a pinned skill) must exist when it is
written, and a skill with ``external_write`` side effects is refused: a rule
has no approval to cite as the basis of an external action (ADR-0056 §4).
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.approval_outcomes import authority_snapshot
from control_plane.application.commands.goals import require_linkable_goal
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import Permission, SkillSideEffects
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.domain.work_rules import (
    RuleSpec,
    RuleStatus,
    TriggerKind,
    normalize_description,
    normalize_rule_key,
    normalize_rule_spec,
)
from control_plane.infrastructure.db.models import Skill, TaskType, WorkRule

_UNSET: Any = object()

# The journal consumer of the rule engine (event_consumer_cursors.name).
RULES_CONSUMER = "work-rules"


def rule_scope(workspace_id: uuid.UUID | None) -> ResourceRef | None:
    """Where ``rules.*`` is decided: the rule's workspace, or the tenant."""
    return ResourceRef("workspace", str(workspace_id)) if workspace_id else None


async def get_tenant_rule(
    session: AsyncSession, ctx: AuthContext, rule_id: uuid.UUID, *, for_update: bool = False
) -> WorkRule:
    stmt = select(WorkRule).where(WorkRule.id == rule_id, WorkRule.tenant_id == ctx.tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    rule = await session.scalar(stmt)
    if rule is None:
        raise NotFoundError("Rule not found", details={"ruleId": str(rule_id)})
    return rule


async def ensure_rule_cursor(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """The tenant's journal cursor of the rule engine, created at the present.

    Placed after the newest event below the stable horizon: everything the
    consumer will ever read after it commits later than this point. A rule
    never looks at the past it was not enabled for (``enabled_at``), so
    replaying the tenant's history would only be work that matches nothing.
    """
    await session.execute(
        text(
            """
            INSERT INTO event_consumer_cursors
                (name, tenant_id, tx_id, sequence, updated_at, metadata, failure_count)
            SELECT :name, :tenant, COALESCE(MAX(p.tx_id), 0),
                   COALESCE((ARRAY_AGG(p.sequence ORDER BY p.tx_id DESC, p.sequence DESC))[1], 0),
                   now(), '{}'::jsonb, 0
              FROM (
                    (SELECT e.tx_id, e.sequence FROM events e
                      WHERE e.tenant_id = :tenant
                        AND e.tx_id < pg_snapshot_xmin(pg_current_snapshot())::text::bigint
                      ORDER BY e.tx_id DESC, e.sequence DESC LIMIT 1)
                    UNION ALL
                    (SELECT a.tx_id, a.sequence FROM event_archive a
                      WHERE a.tenant_id = :tenant
                      ORDER BY a.tx_id DESC, a.sequence DESC LIMIT 1)
                   ) p
            ON CONFLICT (name, tenant_id) DO NOTHING
            """
        ),
        {"name": RULES_CONSUMER, "tenant": tenant_id},
    )


async def _check_references(session: AsyncSession, ctx: AuthContext, spec: RuleSpec) -> None:
    """The task type and the skill a rule names exist now (typos fail early)."""
    type_key = spec.action.get("taskType")
    if type_key is not None:
        found = await session.scalar(
            select(func.count())
            .select_from(TaskType)
            .where(TaskType.tenant_id == ctx.tenant_id, TaskType.key == type_key)
        )
        if not found:
            raise ValidationError(
                "unknown_task_type",
                f"Task type {type_key!r} is not registered",
                details={"field": "action.taskType", "taskType": type_key},
            )
    if spec.interpretation is not None:
        ref = spec.interpretation["skill"]
        name, version = ref.split("@", 1)
        skill = await session.scalar(
            select(Skill).where(
                Skill.tenant_id == ctx.tenant_id, Skill.name == name, Skill.version == version
            )
        )
        if skill is None:
            raise ValidationError(
                "unknown_skill",
                f"Skill {ref!r} is not registered",
                details={"field": "interpretation.skill", "skill": ref},
            )
        if skill.side_effects == SkillSideEffects.EXTERNAL_WRITE:
            raise ValidationError(
                "rule_skill_side_effects",
                "A rule interprets facts; it cannot call an external_write skill "
                "(it has no approval to cite as the basis)",
                details={"field": "interpretation.skill", "skill": ref},
            )


def _validate_status(status: Any) -> str:
    if status not in (RuleStatus.ENABLED, RuleStatus.DISABLED):
        raise ValidationError(
            "invalid_rule", "status must be 'enabled' or 'disabled'", details={"field": "status"}
        )
    return str(status)


def _schedule_next_run(rule: WorkRule) -> None:
    """A schedule rule runs at the next pass after being enabled, then every N seconds."""
    if rule.status == RuleStatus.ENABLED and rule.trigger.get("kind") == TriggerKind.SCHEDULE:
        rule.next_run_at = rule.next_run_at or utcnow()
    else:
        rule.next_run_at = None


def _take_authority(rule: WorkRule, ctx: AuthContext) -> None:
    rule.authority = authority_snapshot(ctx)
    rule.authority_principal_id = ctx.principal_id


def _summary(rule: WorkRule) -> dict[str, Any]:
    """What the journal says about a rule: shape and references, no templates."""
    return {
        "key": rule.key,
        "version": rule.version,
        "status": rule.status,
        "workspaceId": str(rule.workspace_id) if rule.workspace_id else None,
        "goalId": str(rule.goal_id) if rule.goal_id else None,
        "trigger": {"kind": rule.trigger.get("kind"), "type": rule.trigger.get("type")},
        "skill": (rule.interpretation or {}).get("skill"),
        "action": {"kind": rule.action.get("kind"), "taskType": rule.action.get("taskType")},
    }


async def _record(
    session: AsyncSession,
    ctx: AuthContext,
    rule: WorkRule,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="rule",
        entity_id=rule.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"ruleId": str(rule.id), **payload},
    )


async def _require_free_key(session: AsyncSession, ctx: AuthContext, key: str) -> None:
    # Serialized per (tenant, key): the partial unique index would otherwise
    # turn a concurrent twin into a 500 instead of this 409.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"work-rule:{ctx.tenant_id}:{key}")))
    )
    taken = await session.scalar(
        select(WorkRule.id).where(
            WorkRule.tenant_id == ctx.tenant_id,
            WorkRule.key == key,
            WorkRule.status != RuleStatus.ARCHIVED,
        )
    )
    if taken is not None:
        raise ConflictError(
            "rule_key_taken",
            "A rule with this key already exists",
            details={"key": key, "ruleId": str(taken)},
        )


async def create_rule(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    trigger: dict[str, Any],
    action: dict[str, Any],
    condition: Any = None,
    interpretation: dict[str, Any] | None = None,
    description: str | None = None,
    workspace_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    status: str = RuleStatus.ENABLED,
) -> WorkRule:
    from control_plane.application.commands.workspaces import require_active_workspace

    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(workspace_id))
    rule_key = normalize_rule_key(key)
    text_description = normalize_description(description)
    spec = normalize_rule_spec(
        trigger=trigger, condition=condition, interpretation=interpretation, action=action
    )
    rule_status = _validate_status(status)
    if workspace_id is not None:
        await require_active_workspace(session, ctx, workspace_id)
    if goal_id is not None:
        await require_linkable_goal(session, ctx, goal_id, workspace_id=workspace_id)
    await _check_references(session, ctx, spec)
    await _require_free_key(session, ctx, rule_key)

    now = utcnow()
    rule = WorkRule(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        goal_id=goal_id,
        key=rule_key,
        description=text_description,
        version=1,
        status=rule_status,
        trigger=spec.trigger,
        condition=spec.condition,
        interpretation=spec.interpretation,
        action=spec.action,
        authority=None,
        authority_principal_id=None,
        enabled_at=None,
        next_run_at=None,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    if rule_status == RuleStatus.ENABLED:
        _take_authority(rule, ctx)
        rule.enabled_at = now
    _schedule_next_run(rule)
    session.add(rule)
    await session.flush()
    await ensure_rule_cursor(session, ctx.tenant_id)
    await _record(session, ctx, rule, "rule.created", _summary(rule))
    return rule


async def update_rule(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    rule_id: uuid.UUID,
    expected_version: int,
    description: str | Any = _UNSET,
    trigger: dict[str, Any] | Any = _UNSET,
    condition: Any = _UNSET,
    interpretation: dict[str, Any] | Any | None = _UNSET,
    action: dict[str, Any] | Any = _UNSET,
    goal_id: uuid.UUID | Any | None = _UNSET,
) -> WorkRule:
    """Change what a rule does; the key and the workspace are its identity.

    A change of an enabled rule moves its authority to the writer: whoever
    decided what the rule does now answers for it. A PATCH that restates the
    current values is not a change (no new version, no event).
    """
    await authorize(ctx, Permission.RULES_WRITE)
    rule = await get_tenant_rule(session, ctx, rule_id, for_update=True)
    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(rule.workspace_id))
    _require_live(rule)
    if rule.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Rule version does not match If-Match",
            details={
                "ruleId": str(rule.id),
                "expectedVersion": expected_version,
                "currentVersion": rule.version,
            },
        )
    provided = [
        v
        for v in (description, trigger, condition, interpretation, action, goal_id)
        if v is not _UNSET
    ]
    if not provided:
        raise ValidationError("empty_update", "No fields to update")

    spec = normalize_rule_spec(
        trigger=rule.trigger if trigger is _UNSET else trigger,
        condition=rule.condition if condition is _UNSET else condition,
        interpretation=rule.interpretation if interpretation is _UNSET else interpretation,
        action=rule.action if action is _UNSET else action,
    )
    changes: dict[str, Any] = {
        "trigger": spec.trigger,
        "condition": spec.condition,
        "interpretation": spec.interpretation,
        "action": spec.action,
    }
    if description is not _UNSET:
        changes["description"] = normalize_description(description)
    if goal_id is not _UNSET:
        if goal_id is not None:
            await require_linkable_goal(session, ctx, goal_id, workspace_id=rule.workspace_id)
        changes["goal_id"] = goal_id
    changes = {k: v for k, v in changes.items() if getattr(rule, k) != v}
    if not changes:
        return rule
    await _check_references(session, ctx, spec)

    for field_name, value in changes.items():
        setattr(rule, field_name, value)
    if "trigger" in changes:
        rule.next_run_at = None
        _schedule_next_run(rule)
    if rule.status == RuleStatus.ENABLED:
        _take_authority(rule, ctx)
    rule.version += 1
    rule.updated_at = utcnow()
    await session.flush()
    await _record(
        session,
        ctx,
        rule,
        "rule.updated",
        {"changes": sorted(_camel(k) for k in changes), **_summary(rule)},
    )
    return rule


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _require_live(rule: WorkRule) -> None:
    if rule.status == RuleStatus.ARCHIVED:
        raise ConflictError(
            "rule_archived",
            "An archived rule cannot be changed or enabled",
            details={"ruleId": str(rule.id)},
        )


async def set_rule_status(
    session: AsyncSession, ctx: AuthContext, *, rule_id: uuid.UUID, status: str
) -> WorkRule:
    """Enable or disable a rule; repeating the current state changes nothing.

    Enabling takes the caller's authority and starts the rule at the present:
    facts recorded while it was disabled are not evaluated after the fact.
    """
    await authorize(ctx, Permission.RULES_WRITE)
    rule = await get_tenant_rule(session, ctx, rule_id, for_update=True)
    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(rule.workspace_id))
    _require_live(rule)
    target = _validate_status(status)
    if rule.status == target:
        return rule
    now = utcnow()
    rule.status = target
    if target == RuleStatus.ENABLED:
        _take_authority(rule, ctx)
        rule.enabled_at = now
        await ensure_rule_cursor(session, ctx.tenant_id)
    rule.next_run_at = None
    _schedule_next_run(rule)
    rule.updated_at = now
    await session.flush()
    await _record(
        session,
        ctx,
        rule,
        "rule.enabled" if target == RuleStatus.ENABLED else "rule.disabled",
        _summary(rule),
    )
    return rule


async def archive_rule(session: AsyncSession, ctx: AuthContext, *, rule_id: uuid.UUID) -> None:
    """Take a rule out of service for good (``DELETE``); its history stays.

    The work it filed is not touched; evaluations still waiting on a skill
    end as ``skipped``. The key becomes free for a new rule.
    """
    await authorize(ctx, Permission.RULES_WRITE)
    rule = await get_tenant_rule(session, ctx, rule_id, for_update=True)
    await authorize(ctx, Permission.RULES_WRITE, resource=rule_scope(rule.workspace_id))
    if rule.status == RuleStatus.ARCHIVED:
        return
    rule.status = RuleStatus.ARCHIVED
    rule.next_run_at = None
    rule.updated_at = utcnow()
    await session.flush()
    await _record(session, ctx, rule, "rule.archived", _summary(rule))


def schedule_slot(rule: WorkRule) -> tuple[str, Any]:
    """The trigger ref of a due schedule and the rule's next run time."""
    every = timedelta(seconds=int(rule.trigger["everySeconds"]))
    assert rule.next_run_at is not None
    due = rule.next_run_at
    now = utcnow()
    upcoming = due + every
    # A worker that was down does not replay every missed slot: one run now.
    if upcoming <= now:
        upcoming = now + every
    return f"schedule:{int(due.timestamp())}", upcoming
