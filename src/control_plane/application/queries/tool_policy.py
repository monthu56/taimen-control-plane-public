"""Effective Tool Policy and the scoped Tool Discovery View (HRS-3).

The catalog says what the runtime technically knows; the policy says what this
principal, run and workspace may use right now; the view is a bounded
projection of the intersection. Only the first is stored — the other two are
recomputed on every read, because a cached authorization decision is a stale
authorization decision.

Two properties are structural rather than conditional, so no future edit can
drop them by removing a filter:

* the search statement JOINs ``principal_skills``, so a tool that is not
  assigned is never even loaded, let alone projected;
* every statement is filtered by ``tenant_id`` at the source.
"""

import dataclasses
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import decode_cursor, encode_cursor, utcnow
from control_plane.application.queries.projects import (
    effective_config_for,
    get_tenant_project,
    project_for_workspace,
)
from control_plane.domain.child_handle import grant_from_stored
from control_plane.domain.enums import Permission, SessionStatus, SkillStatus
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.tool_discovery import (
    ProjectPolicyInput,
    ToolCandidate,
    ToolDecision,
    catalog_revision,
    clamp_tool_limit,
    decide_visibility,
    normalize_harness_protocols,
    policy_revision,
    project_detail,
    project_summary,
    validate_query,
    view_hash,
)
from control_plane.infrastructure.db.models import (
    PrincipalSkill,
    Run,
    RunChildHandle,
    Session,
    Skill,
    Task,
)

#: Free-text terms per query. All must match (AND): a long query narrows the
#: result instead of widening it, which is what makes an unbounded catalog
#: searchable rather than merely samplable.
MAX_QUERY_TERMS = 8


@dataclass(frozen=True)
class EffectiveToolPolicy:
    """Everything needed to decide, plus the revisions that decided it."""

    principal_id: uuid.UUID
    allowed_protocols: list[str] | None
    harness_protocols: frozenset[str] | None
    catalog_revision: str
    policy_revision: str
    project_id: uuid.UUID | None
    #: Ceiling of a run launched by a parent (HRS-7), as ``name@version`` refs.
    #: ``None`` means the run has no handle and is not narrowed — which is not
    #: the same as an empty grant.
    granted_skills: frozenset[str] | None = None


async def project_policy_for_task(
    session: AsyncSession, ctx: AuthContext, task: Task
) -> ProjectPolicyInput:
    """Effective project configuration, or an honest 'absent' if there is none."""
    if task.workspace_id is None:
        return ProjectPolicyInput()
    project_id = await project_for_workspace(session, ctx.tenant_id, task.workspace_id)
    if project_id is None:
        return ProjectPolicyInput()
    project = await get_tenant_project(session, ctx, project_id)
    effective = await effective_config_for(session, ctx.tenant_id, project)
    layers = effective.provenance.get("layers") or []
    own_layer = layers[-1] if layers else {}
    return ProjectPolicyInput(
        project_id=str(project.id),
        active_revision=own_layer.get("revision"),
        governance=dict(effective.config.get("governance") or {}),
    )


def allowed_protocols_of(project_policy: ProjectPolicyInput) -> list[str] | None:
    """``allowedSkillProtocols`` as a list, or ``None`` for "no restriction"."""
    allowed = project_policy.governance.get("allowedSkillProtocols")
    return [str(item) for item in allowed] if isinstance(allowed, list) else None


async def tenant_catalog_revision(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Hash of the tenant's whole catalog — including tools nobody can see.

    Deliberately wider than the view: a tool becoming assignable, deprecated or
    deleted must invalidate cached projections even though it was invisible a
    moment ago.
    """
    rows = (
        await session.execute(
            select(Skill.id, Skill.row_version, Skill.status).where(Skill.tenant_id == tenant_id)
        )
    ).all()
    return catalog_revision((str(row[0]), int(row[1]), str(row[2])) for row in rows)


async def _assigned_skill_ids(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = (
        await session.scalars(
            select(PrincipalSkill.skill_id).where(
                PrincipalSkill.tenant_id == tenant_id,
                PrincipalSkill.principal_id == principal_id,
            )
        )
    ).all()
    return list(rows)


async def _harness_protocols(
    session: AsyncSession, ctx: AuthContext, run: Run | None
) -> frozenset[str] | None:
    """Protocols declared by the run's session, or by the newest active one."""
    work_session: Session | None = None
    if run is not None:
        work_session = await session.get(Session, run.session_id)
    else:
        work_session = await session.scalar(
            select(Session)
            .where(
                Session.tenant_id == ctx.tenant_id,
                Session.principal_id == ctx.principal_id,
                Session.status == SessionStatus.ACTIVE,
                Session.expires_at > utcnow(),
            )
            .order_by(Session.started_at.desc())
            .limit(1)
        )
    if work_session is None:
        return None
    return normalize_harness_protocols(work_session.harness_capabilities)


async def _run_for_scope(session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID) -> Run:
    run = await session.scalar(
        select(Run).where(
            Run.id == run_id,
            Run.tenant_id == ctx.tenant_id,
            Run.principal_id == ctx.principal_id,
        )
    )
    if run is None:
        # Another principal's run is reported exactly like a missing one: the
        # existence of a run is not something discovery may confirm.
        raise NotFoundError("Run not found", details={"runId": str(run_id)})
    return run


async def _child_grant_skills(
    session: AsyncSession, ctx: AuthContext, run: Run | None
) -> frozenset[str] | None:
    """Skill ceiling of a run that was launched by a parent (HRS-7).

    Read here rather than enforced separately so that search, describe and
    the invocation gate all narrow through the same decision — a
    second enforcement point would eventually disagree with this one.
    """
    if run is None:
        return None
    handle = await session.scalar(
        select(RunChildHandle).where(
            RunChildHandle.tenant_id == ctx.tenant_id,
            RunChildHandle.child_run_id == run.id,
        )
    )
    if handle is None:
        return None
    return frozenset(grant_from_stored(handle.granted).skills)


async def resolve_effective_tool_policy(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID | None = None,
) -> EffectiveToolPolicy:
    """What this principal may use right now, and the revisions that say so."""
    run = await _run_for_scope(session, ctx, run_id) if run_id is not None else None

    project_policy = ProjectPolicyInput()
    if run is not None:
        task = await session.get(Task, run.task_id)
        if task is not None:
            project_policy = await project_policy_for_task(session, ctx, task)

    allowed = allowed_protocols_of(project_policy)
    harness_protocols = await _harness_protocols(session, ctx, run)
    granted_skills = await _child_grant_skills(session, ctx, run)
    assigned = await _assigned_skill_ids(session, ctx.tenant_id, ctx.principal_id)
    catalog = await tenant_catalog_revision(session, ctx.tenant_id)
    policy = policy_revision(
        principal_id=str(ctx.principal_id),
        assigned_skill_ids=[str(skill_id) for skill_id in assigned],
        allowed_protocols=allowed,
        project_revision=(
            None
            if project_policy.project_id is None
            else f"{project_policy.project_id}@{project_policy.active_revision}"
        ),
        harness_protocols=harness_protocols,
        granted_skills=granted_skills,
    )
    return EffectiveToolPolicy(
        principal_id=ctx.principal_id,
        allowed_protocols=allowed,
        harness_protocols=harness_protocols,
        catalog_revision=catalog,
        policy_revision=policy,
        project_id=(uuid.UUID(project_policy.project_id) if project_policy.project_id else None),
        granted_skills=granted_skills,
    )


def _candidate(skill: Skill) -> ToolCandidate:
    return ToolCandidate(
        skill_id=str(skill.id),
        name=skill.name,
        version=skill.version,
        protocol=skill.protocol,
        status=skill.status,
        row_version=skill.row_version,
        description=skill.description or "",
        input_schema=skill.input_schema,
        assigned=True,
    )


def _decide(candidate: ToolCandidate, policy: EffectiveToolPolicy) -> ToolDecision:
    return decide_visibility(
        candidate,
        harness_protocols=policy.harness_protocols,
        allowed_protocols=policy.allowed_protocols,
        granted_skills=policy.granted_skills,
    )


async def decide_for_skill(
    session: AsyncSession, ctx: AuthContext, skill: Skill, policy: EffectiveToolPolicy
) -> ToolDecision:
    """The shared decision for one already-resolved skill version.

    Unlike ``resolve_tool_decision`` the skill is not looked up through the
    assignment join: the caller already holds the row (skill invocation
    resolves by reference with ADR-0021 rules), so assignment is read here and
    fed to the same ``decide_visibility`` as every other gate.
    """
    assigned = await session.scalar(
        select(PrincipalSkill.id).where(
            PrincipalSkill.tenant_id == ctx.tenant_id,
            PrincipalSkill.principal_id == ctx.principal_id,
            PrincipalSkill.skill_id == skill.id,
        )
    )
    candidate = _candidate(skill)
    if assigned is None:
        candidate = dataclasses.replace(candidate, assigned=False)
    return _decide(candidate, policy)


def _assigned_statement(ctx: AuthContext) -> Select[tuple[Skill]]:
    """Assigned, non-disabled skills of this principal — the only search base."""
    return (
        select(Skill)
        .join(PrincipalSkill, PrincipalSkill.skill_id == Skill.id)
        .where(
            Skill.tenant_id == ctx.tenant_id,
            PrincipalSkill.tenant_id == ctx.tenant_id,
            PrincipalSkill.principal_id == ctx.principal_id,
            Skill.status != SkillStatus.DISABLED,
        )
    )


def _apply_query(stmt: Select[tuple[Skill]], query: str) -> Select[tuple[Skill]]:
    terms = query.split()
    if len(terms) > MAX_QUERY_TERMS:
        raise ValidationError(
            "invalid_tool_query",
            f"query must contain at most {MAX_QUERY_TERMS} terms",
            details={"maxTerms": MAX_QUERY_TERMS},
        )
    for term in terms:
        # ESCAPE makes a literal '%' in a term match a literal '%', so a query
        # cannot widen itself into "everything" through wildcard injection.
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            or_(
                Skill.name.ilike(pattern, escape="\\"),
                Skill.description.ilike(pattern, escape="\\"),
            )
        )
    return stmt


async def search_tools(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    query: str | None = None,
    run_id: uuid.UUID | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One bounded page of the discovery view, plus the revisions behind it.

    An empty query is eager mode: the same dataset, the same code path, the
    first page. Having one implementation is what makes the eager/search
    comparison in the verification report a measurement rather than a claim.
    """
    await authorize(ctx, Permission.TASKS_READ)
    text = validate_query(query)
    page_limit = clamp_tool_limit(limit)
    policy = await resolve_effective_tool_policy(session, ctx, run_id=run_id)

    stmt = _assigned_statement(ctx)
    if text:
        stmt = _apply_query(stmt, text)
    if cursor is not None:
        data = decode_cursor(cursor)
        try:
            after_name = str(data["n"])
            after_id = uuid.UUID(data["i"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
        stmt = stmt.where(tuple_(Skill.name, Skill.id) > (after_name, after_id))
    stmt = stmt.order_by(Skill.name.asc(), Skill.id.asc()).limit(page_limit + 1)

    rows = list((await session.scalars(stmt)).all())
    has_more = len(rows) > page_limit
    rows = rows[:page_limit]

    items: list[dict[str, Any]] = []
    for skill in rows:
        candidate = _candidate(skill)
        decision = _decide(candidate, policy)
        if not decision.authorized:
            # Governance said no. Unlike a capability mismatch this is not the
            # caller's own configuration to fix, and naming it would describe a
            # tool the caller may not use.
            continue
        items.append(project_summary(candidate, decision))

    next_cursor = (
        encode_cursor({"n": rows[-1].name, "i": str(rows[-1].id)}) if has_more and rows else None
    )
    return {
        "items": items,
        "nextCursor": next_cursor,
        "view": {
            "catalogRevision": policy.catalog_revision,
            "policyRevision": policy.policy_revision,
            "viewHash": view_hash(
                catalog=policy.catalog_revision,
                policy=policy.policy_revision,
                query=text,
                limit=page_limit,
                cursor=cursor,
            ),
            "mode": "search" if text else "eager",
            "returned": len(items),
            "hasMore": has_more,
        },
    }


def _reference_statement(stmt: Select[tuple[Skill]], tool_ref: str) -> Select[tuple[Skill]]:
    """Resolve a UUID, ``name`` or ``name@version`` reference."""
    try:
        skill_id = uuid.UUID(tool_ref)
    except ValueError:
        name, _, version = tool_ref.partition("@")
        stmt = stmt.where(Skill.name == name)
        if version:
            stmt = stmt.where(Skill.version == version)
        return stmt.order_by(Skill.created_at.desc()).limit(1)
    return stmt.where(Skill.id == skill_id)


async def resolve_tool_decision(
    session: AsyncSession,
    ctx: AuthContext,
    tool_ref: str,
    policy: EffectiveToolPolicy,
) -> tuple[Skill, ToolCandidate, ToolDecision]:
    """Resolve an assigned tool and return the shared authorization decision.

    The caller decides how to surface an authorization denial. Read-side
    discovery deliberately collapses every denial to ``tool_not_found``;
    invocation may preserve a child-handle ceiling violation without deriving
    a second policy decision.
    """
    skill = await session.scalar(_reference_statement(_assigned_statement(ctx), tool_ref))
    if skill is None:
        raise NotFoundError("Tool not found", details={"tool": tool_ref})
    candidate = _candidate(skill)
    decision = _decide(candidate, policy)
    return skill, candidate, decision


async def resolve_authorized_tool(
    session: AsyncSession,
    ctx: AuthContext,
    tool_ref: str,
    policy: EffectiveToolPolicy,
) -> tuple[Skill, ToolCandidate, ToolDecision]:
    """Resolve a visible tool without exposing why an invisible one was denied.

    A tool the caller may not use and a tool that does not exist take the same
    path and produce the same ``NotFoundError``: telling them apart would turn
    ``GET /tools/{name}`` into an enumeration oracle over the tenant's catalog.
    """
    skill, candidate, decision = await resolve_tool_decision(session, ctx, tool_ref, policy)
    if not decision.authorized:
        raise NotFoundError("Tool not found", details={"tool": tool_ref})
    return skill, candidate, decision


async def describe_tool(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    tool_ref: str,
    run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Full (sanitized) projection of one tool the caller may use."""
    await authorize(ctx, Permission.TASKS_READ)
    policy = await resolve_effective_tool_policy(session, ctx, run_id=run_id)
    _, candidate, decision = await resolve_authorized_tool(session, ctx, tool_ref, policy)
    detail = project_detail(candidate, decision)
    detail["view"] = {
        "catalogRevision": policy.catalog_revision,
        "policyRevision": policy.policy_revision,
    }
    return detail
