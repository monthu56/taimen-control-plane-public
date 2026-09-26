"""Feedback on an attention item: ``POST /me/attention/{itemKey}:feedback`` (CP-ADR-0071).

A principal judges only an item that is on its own list right now: the item is
recomputed by its rule, so a key of someone else's object — or of one that has
already left the list — is 404 and nothing is written. The verdict is kept
with the rule version, reason and score the item had, one row per
``(principal, itemKey)``; a repeated verdict replaces the previous one. It
changes nothing in the list itself (§4): what a rule selects changes only
with a new version of the rule.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.queries.attention import find_item
from control_plane.infrastructure.db.models import AttentionFeedback


@dataclass(frozen=True)
class FeedbackResult:
    feedback: AttentionFeedback
    created: bool


async def record_feedback(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    item_key: str,
    verdict: str,
    comment: str | None = None,
) -> FeedbackResult:
    # find_item authorizes by the rule's read permission and proves the item
    # is the caller's: there is no separate right to judge one's own list.
    item = await find_item(session, ctx, item_key)
    now = utcnow()
    existing = await session.scalar(
        select(AttentionFeedback.id).where(
            AttentionFeedback.tenant_id == ctx.tenant_id,
            AttentionFeedback.principal_id == ctx.principal_id,
            AttentionFeedback.item_key == item.item_key,
        )
    )
    values = {
        "rule_key": item.rule_key,
        "rule_version": item.rule_version,
        "kind": item.kind,
        "reason_code": item.reason_code,
        "entity_type": item.entity_type,
        "entity_id": item.entity_id,
        "score": item.score,
        "verdict": verdict,
        "comment": comment or "",
        "updated_at": now,
    }
    # One statement for both paths: two concurrent verdicts on the same item
    # end as one row with the later verdict, never as a unique violation.
    stmt = (
        insert(AttentionFeedback)
        .values(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            item_key=item.item_key,
            created_at=now,
            **values,
        )
        .on_conflict_do_update(constraint="uq_attention_feedback_item", set_=values)
        .returning(AttentionFeedback)
    )
    row = (await session.scalars(stmt, execution_options={"populate_existing": True})).one()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="attention.feedback_recorded",
        entity_type="attention_feedback",
        entity_id=row.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "principalId": str(ctx.principal_id),
            "itemKey": item.item_key,
            "rule": item.rule,
            "ruleKey": item.rule_key,
            "ruleVersion": item.rule_version,
            "kind": item.kind,
            "reasonCode": item.reason_code,
            "entityType": item.entity_type,
            "entityId": str(item.entity_id),
            "score": item.score,
            "verdict": verdict,
            "created": existing is None,
            "hasComment": bool(comment),
        },
    )
    return FeedbackResult(feedback=row, created=existing is None)
