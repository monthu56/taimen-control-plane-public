"""Row-lock discipline shared by every writer (CP-ADR-0077 §3).

``principals/{id}:disable`` locks, in this order: the agent record
(``FOR SHARE``), the principal (``FOR UPDATE``), its bindings and delegations,
its sessions and those opened on its behalf (``FOR UPDATE``), then task → claim
→ run, and skill call rows last. A writer that holds any of the later rows and
then asks for an earlier one closes a cycle with it — and inserting or
re-pointing a row that references a principal or a session asks for exactly
that: Postgres checks the foreign key by taking ``FOR KEY SHARE`` on the
referenced row, *at the moment of the insert*, normally after the task.

The rule that keeps every writer on the right side of that order:

1. **A write transaction takes its caller's principal ``FOR KEY SHARE`` before
   any other row lock** (``lock_caller``; the HTTP write flow does it as the
   first statement of every mutating request, the worker paths as soon as they
   know on whose authority they act). The foreign-key lock on the caller that
   comes later is then already held and costs nothing; a ``:disable`` of the
   caller either waits on its first lock or makes the write wait on its first
   statement, holding nothing.
2. **A writer that touches a run or a claim takes that run's (claim's) session
   ``FOR KEY SHARE`` before the task** (``lock_session_key_share``). The ids
   are immutable, so they are read without a lock first. A session opened on
   behalf of a human is closed by ``:disable`` of that human, which is not the
   caller: rule 1 does not cover it, rule 2 does.
3. **A principal other than the caller that a write references (an assignee
   of a gate approval, the owner of a child task) is locked ``FOR KEY SHARE``
   before the task too** — ``lock_principals_key_share``.

The caller's lock also re-reads its status: auth resolved the principal
before the transaction began, and a ``:disable`` committed in between must not
let the request open a session or take a claim for a principal that is no
longer active.
"""

import uuid
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.domain.enums import PrincipalStatus
from control_plane.domain.errors import AuthorizationError
from control_plane.infrastructure.db.models import (
    Agent,
    Principal,
    ProcessDefinition,
    Session,
    TaskClaim,
    WorkRule,
)


async def lock_principal_key_share(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> str | None:
    """``FOR KEY SHARE`` on a principal; returns its status as of the lock."""
    status: str | None = await session.scalar(
        select(Principal.status)
        .where(Principal.id == principal_id, Principal.tenant_id == tenant_id)
        .with_for_update(key_share=True)
    )
    return status


async def lock_principals_key_share(
    session: AsyncSession, tenant_id: uuid.UUID, principal_ids: Iterable[uuid.UUID | None]
) -> None:
    """``FOR KEY SHARE`` on principals a write will reference (rule 3).

    One statement, rows locked in id order (the lock is taken above the sort),
    so two such calls over overlapping sets meet on the lowest common id.
    """
    ids = sorted({p for p in principal_ids if p is not None})
    if not ids:
        return
    await session.execute(
        select(Principal.id)
        .where(Principal.tenant_id == tenant_id, Principal.id.in_(ids))
        .order_by(Principal.id)
        .with_for_update(key_share=True)
    )


async def lock_rule_principals(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Rule 1 for a batch of rule evaluations: every principal a rule may act as.

    A batch (one journal page of a tenant) runs many evaluations in one
    transaction, each in its own savepoint — and row locks of a savepoint that
    is released stay until the transaction commits. The first evaluation may
    lock a task; a later one acting as another principal would then take that
    principal after the task. So the batch takes all of them first: the
    enabler of every enabled rule and the principal of every rule's agent.
    """
    enablers = await session.scalars(
        select(WorkRule.authority_principal_id).where(
            WorkRule.tenant_id == tenant_id, WorkRule.authority_principal_id.is_not(None)
        )
    )
    agents = await session.scalars(
        select(Agent.principal_id).where(
            Agent.tenant_id == tenant_id,
            Agent.principal_id.is_not(None),
            Agent.key.in_(
                select(WorkRule.identity_agent_key).where(
                    WorkRule.tenant_id == tenant_id, WorkRule.identity_agent_key.is_not(None)
                )
            ),
        )
    )
    await lock_principals_key_share(session, tenant_id, [*enablers, *agents])


def _assign_chain_refs(spec: Any) -> tuple[set[uuid.UUID], set[str]]:
    """Every static ``principal`` id and ``agent`` key a spec names, anywhere.

    Assign chains (``assign`` of tasks and retrospectives, ``approvers``,
    ``to`` of escalations), ``call: {agent}`` steps (the engine builds their
    chain itself), ``identity.agent`` — collected under any key: a string that
    is not an agent key or a principal id locks nothing, and one taken without
    need is harmless. An ``expr`` item is only known when the step runs and is
    not collected (CP-ADR-0077 §3, exception).
    """
    principals: set[uuid.UUID] = set()
    agents: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "principal" and isinstance(value, str):
                    with suppress(ValueError):
                        principals.add(uuid.UUID(value))
                elif key == "agent" and isinstance(value, str):
                    agents.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    return principals, agents


async def lock_process_identities(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    extra: Iterable[uuid.UUID | None] = (),
) -> None:
    """Rule 1 and 3 for the process engine: every principal a process may use.

    A process acts as its agent, and one step may start or cancel a child
    process — acting as the child's agent — after its intents locked tasks;
    its steps assign tasks and ask approvers named in the definition; a
    journal batch feeds many instances in one transaction. So, over every
    version of every definition of the tenant (a child being cancelled may run
    an old one): the identity agents' principals and the static ``principal``
    and ``agent`` references anywhere in the spec, plus ``extra`` (the actors that
    start instances in a batch) — whatever the engine does next, the
    principals it may reference are already held.

    Taken once per (sub)transaction: a second call in the same one returns at
    once (a definition or agent published in between is not re-read — the
    window CP-ADR-0077 §3 names).
    """
    extra_ids = [p for p in extra if p is not None]
    # Marked on the innermost (sub)transaction: locks a rolled-back savepoint
    # took are gone with it, so a call is skipped only under the (sub)transaction
    # that took them or one nested in it.
    sync = session.sync_session
    transaction = sync.get_nested_transaction() or sync.get_transaction()
    marker = ("process_identities_locked", tenant_id)
    held = session.info.get(marker)
    enclosing = transaction
    while enclosing is not None:
        if enclosing is held and enclosing.is_active:
            await lock_principals_key_share(session, tenant_id, extra_ids)
            return
        enclosing = enclosing.parent
    rows = (
        await session.execute(
            select(ProcessDefinition.identity_agent, ProcessDefinition.spec).where(
                ProcessDefinition.tenant_id == tenant_id
            )
        )
    ).all()
    principals: set[uuid.UUID] = set(extra_ids)
    agents: set[str] = set()
    for identity_agent, spec in rows:
        if identity_agent:
            agents.add(identity_agent)
        found_principals, found_agents = _assign_chain_refs(spec)
        principals |= found_principals
        agents |= found_agents
    if agents:
        principals |= {
            p
            for p in await session.scalars(
                select(Agent.principal_id).where(
                    Agent.tenant_id == tenant_id,
                    Agent.principal_id.is_not(None),
                    Agent.key.in_(sorted(agents)),
                )
            )
            if p is not None
        }
    await lock_principals_key_share(session, tenant_id, principals)
    session.info[marker] = transaction


async def lock_caller(session: AsyncSession, ctx: AuthContext) -> None:
    """Rule 1: the caller's principal before anything else, and still active.

    Idempotent within a transaction: a second call re-takes a lock already held.
    """
    status = await lock_principal_key_share(session, ctx.tenant_id, ctx.principal_id)
    if status is not None and status != PrincipalStatus.ACTIVE:
        # A principal disabled while this request was in flight: the lock waited
        # for the ``:disable`` to commit and now reads its outcome.
        raise AuthorizationError(
            "Principal is not active",
            code="principal_not_active",
            details={"principalId": str(ctx.principal_id), "status": status},
        )


async def lock_caller_and_principal_for_update(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> Principal | None:
    """Rule 1 for a command that takes another principal ``FOR UPDATE``.

    ``:disable``, ``:enable`` and ``:retire`` lock their target principal
    ``FOR UPDATE``.
    Were the caller's ``FOR KEY SHARE`` always first, two such commands aimed
    at each other's callers (two administrators disabling one another) would
    each hold its own caller and wait for the other's — the one cycle rule 1
    creates instead of breaking. These commands therefore run without the
    write flow's up-front lock and take both principals here in id order: the
    two commands meet on the lower id, and the one that waits holds nothing
    the other needs. The target comes back locked and freshly read.
    """

    async def target() -> Principal | None:
        found: Principal | None = await session.scalar(
            select(Principal)
            .where(Principal.id == principal_id, Principal.tenant_id == ctx.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return found

    if ctx.principal_id <= principal_id:
        await lock_caller(session, ctx)
        return await target()
    locked = await target()
    await lock_caller(session, ctx)
    return locked


async def lock_session_key_share(
    session: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID | None
) -> None:
    """Rule 2: ``FOR KEY SHARE`` on a work session before any task lock."""
    if session_id is None:
        return
    await session.execute(
        select(Session.id)
        .where(Session.id == session_id, Session.tenant_id == tenant_id)
        .with_for_update(key_share=True)
    )


async def lock_claim_session(
    session: AsyncSession, tenant_id: uuid.UUID, claim_id: uuid.UUID | None
) -> None:
    """Rule 2 for a claim: read its (immutable) session unlocked, then lock it."""
    if claim_id is None:
        return
    session_id = await session.scalar(
        select(TaskClaim.session_id).where(
            TaskClaim.id == claim_id, TaskClaim.tenant_id == tenant_id
        )
    )
    await lock_session_key_share(session, tenant_id, session_id)
