"""Package tests in the core: ``POST /packages:test`` (CP-ADR-0074 §10; process-packages P013).

The body is the package as its files. The core

1. parses the files itself (:mod:`control_plane.domain.package_source`), so a
   finding names the file and the line;
2. builds the package's definitions in memory, never in the catalog: each
   process is checked (§2) against the tenant's catalog with the package's
   own objects over it — a task type, skill, agent, calendar, role or process
   the package brings is known to its processes before it is applied;
3. unless ``checkOnly``, runs the tests ``tests/*.test.yaml`` in the sandbox
   (:mod:`control_plane.domain.process_sandbox`) with the same engine a live
   instance runs, and reports the coverage of every process.

Nothing is written. The transaction of the run is ``READ ONLY`` — the
catalog, roles and calendars are read from it — and a guard on the session
counts every statement that would write; that count is what a test's
``expect: {noSideEffects: true}`` checks. The sandbox gets no client: no
HTTP, no memory, no content store. The one outgoing read is the check of
``governedBy`` against memory (CP-ADR-0076 §7), made after the transaction
closes, for the problems of the package, never for a test.
"""

import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import Any

from sqlalchemy import event, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql.elements import TextClause

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.process_definitions import (
    load_catalog,
    previous_version,
    process_scope,
)
from control_plane.application.commands.process_instances import definition_of, get_instance
from control_plane.application.context.graph import GraphScope
from control_plane.application.queries.process_regulations import (
    governed_by_problems,
    regulation_scope,
)
from control_plane.config import Settings
from control_plane.domain import process_engine as engine
from control_plane.domain import process_sandbox as sandbox
from control_plane.domain.calendar import Calendar
from control_plane.domain.enums import ApprovalStatus, Permission
from control_plane.domain.errors import NotFoundError
from control_plane.domain.package_source import (
    PackageObject,
    PackageTestFile,
    ParsedPackage,
    parse_package,
    select_tests,
)
from control_plane.domain.process_definition import (
    Catalog,
    Problem,
    SkillEntry,
    SpecError,
    check_process,
    governed_references,
    normalized_spec,
    references,
)
from control_plane.infrastructure.context_provider import GraphProvider
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import (
    Approval,
    CalendarVersion,
    ProcessDefinition,
    Role,
    Workspace,
)

# Checks a package Calendar object: the calendar, or the findings of its spec.
CalendarCheck = Callable[[PackageObject], tuple[Calendar | None, list[Problem]]]


@dataclass
class PackageTestReport:
    """``PackageTestOut``."""

    check_only: bool
    problems: list[Problem] = field(default_factory=list)
    tests: list[sandbox.TestResult] = field(default_factory=list)
    coverage: list[sandbox.Coverage] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def status(self) -> str:
        if any(problem.error for problem in self.problems):
            return "invalid"
        if any(test.status != "passed" for test in self.tests):
            return "failed"
        return "passed"

    def out(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checkOnly": self.check_only,
            "problems": [problem.out() for problem in self.problems],
            "tests": [test.out() for test in self.tests],
            "coverage": [item.out() for item in self.coverage],
            "durationMs": self.duration_ms,
        }


# The first keywords of raw SQL that writes; a WITH writes when its body names one.
_WRITE_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "TRUNCATE"})
# Comments, string literals and quoted identifiers: no keyword is read inside them.
_SQL_NOISE = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.DOTALL)
_SQL_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9$]*")


def sql_writes(sql: str) -> bool:
    """Whether raw SQL changes a row: by its first keyword, or a data-modifying ``WITH``."""
    words = [word.upper() for word in _SQL_WORD.findall(_SQL_NOISE.sub(" ", sql))]
    if not words:
        return False
    if words[0] == "WITH":
        # FOR UPDATE and FOR NO KEY UPDATE lock the rows a read selects.
        return any(
            word in _WRITE_KEYWORDS and not (word == "UPDATE" and before in {"FOR", "KEY"})
            for before, word in pairwise(words)
        )
    return words[0] in _WRITE_KEYWORDS


class WriteGuard:
    """Counts the statements of a session that would change a row, while it is attached.

    A write is an ``INSERT``, ``UPDATE`` or ``DELETE`` statement, raw SQL
    (``text(...)``) whose keyword writes (:func:`sql_writes`), and every object
    a flush would insert, update or delete. A read — a ``SELECT`` or a raw
    ``SELECT``/``WITH ... SELECT`` — is not.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session: Session = session.sync_session
        self.count = 0

    def _statement(self, state: ORMExecuteState) -> None:
        statement = state.statement
        if isinstance(statement, TextClause):
            writes = sql_writes(statement.text)
        else:
            writes = state.is_insert or state.is_update or state.is_delete
        if writes:
            self.count += 1

    def _flush(self, session: Session, context: Any, instances: Any) -> None:
        self.count += len(session.new) + len(session.dirty) + len(session.deleted)

    def __enter__(self) -> "WriteGuard":
        event.listen(self.session, "do_orm_execute", self._statement)
        event.listen(self.session, "before_flush", self._flush)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.session, "do_orm_execute", self._statement)
        event.remove(self.session, "before_flush", self._flush)
        self.count += len(self.session.new) + len(self.session.dirty) + len(self.session.deleted)


# --- the package over the catalog -------------------------------------------------------


def _skill_entry(spec: Mapping[str, Any]) -> SkillEntry:
    contract = spec.get("contract")
    if isinstance(contract, dict):
        return SkillEntry(contract.get("inputs"), contract.get("outputs"))
    return SkillEntry(spec.get("inputSchema"), spec.get("outputSchema"))


def overlay_catalog(catalog: Catalog, package: ParsedPackage, calendars: frozenset[str]) -> Catalog:
    """The tenant's catalog with the package's objects over it."""
    skills = dict(catalog.skills)
    for obj in package.of_kind("Skill"):
        skills[f"{obj.key}@{obj.spec.get('version')}"] = _skill_entry(obj.spec)
    task_types = dict(catalog.task_types)
    for obj in package.of_kind("TaskType"):
        task_types[obj.key] = obj.spec.get("fieldSchema") or None
    agents = catalog.agents | {obj.key for obj in package.of_kind("Agent")}
    artifact_types = catalog.artifact_types
    if artifact_types is not None:
        artifact_types = artifact_types | {obj.key for obj in package.of_kind("ArtifactType")}
    processes = catalog.processes
    if processes is not None:
        processes = processes | {obj.key for obj in package.of_kind("Process")}
    return replace(
        catalog,
        skills=skills,
        task_types=task_types,
        agents=agents,
        calendars=catalog.calendars | calendars,
        artifact_types=artifact_types,
        processes=processes,
    )


def with_workspace(spec: dict[str, Any], workspace_id: uuid.UUID | None) -> dict[str, Any]:
    """An install variable ``${…}`` in ``workspaceId`` is the workspace of the run."""
    raw = spec.get("workspaceId")
    if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
        body = {k: v for k, v in spec.items() if k != "workspaceId"}
        if workspace_id is not None:
            body["workspaceId"] = str(workspace_id)
        return body
    return spec


async def _latest_calendars(
    session: AsyncSession, tenant_id: uuid.UUID, keys: set[str]
) -> dict[str, Calendar]:
    if not keys:
        return {}
    latest = (
        select(CalendarVersion.key, func.max(CalendarVersion.version).label("version"))
        .where(CalendarVersion.tenant_id == tenant_id, CalendarVersion.key.in_(sorted(keys)))
        .group_by(CalendarVersion.key)
        .subquery()
    )
    rows = await session.scalars(
        select(CalendarVersion)
        .join(
            latest,
            (CalendarVersion.key == latest.c.key) & (CalendarVersion.version == latest.c.version),
        )
        .where(CalendarVersion.tenant_id == tenant_id)
    )
    return {row.key: Calendar.from_spec(row.spec) for row in rows}


async def _role_slugs(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> frozenset[str]:
    stmt = select(Role.slug).where(Role.tenant_id == tenant_id)
    if workspace_id is None:
        stmt = stmt.where(Role.workspace_id.is_(None))
    else:
        stmt = stmt.where(or_(Role.workspace_id.is_(None), Role.workspace_id == workspace_id))
    rows = await session.scalars(stmt)
    return frozenset(rows.all())


async def workspace_exists(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID
) -> bool:
    found = await session.scalar(
        select(Workspace.id).where(
            Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id
        )
    )
    return found is not None


async def _catalog_processes(
    session: AsyncSession, tenant_id: uuid.UUID, keys: set[str]
) -> dict[str, engine.Definition]:
    """The latest published versions of processes the package calls but does not bring."""
    found: dict[str, engine.Definition] = {}
    for key in sorted(keys):
        row = await session.scalar(
            select(ProcessDefinition)
            .where(ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key == key)
            .order_by(ProcessDefinition.version.desc())
            .limit(1)
        )
        if row is not None:
            found[key] = await definition_of(session, row)
    return found


@dataclass
class _Checked:
    obj: PackageObject
    spec: dict[str, Any]
    catalog: Catalog
    definition: engine.Definition | None = None
    scope: GraphScope | None = None


# --- the route's work ----------------------------------------------------------------------


async def run_package_tests(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: AuthContext,
    settings: Settings,
    provider: GraphProvider | None,
    *,
    files: Sequence[tuple[str, str]],
    tests: Sequence[str] | None,
    workspace_id: uuid.UUID | None,
    check_only: bool,
    check_calendar: CalendarCheck,
) -> PackageTestReport:
    """Check a package and, unless ``check_only``, run its tests; nothing is written."""
    started = time.monotonic()
    report = PackageTestReport(check_only=check_only)
    package = parse_package(files)
    report.problems.extend(package.problems)
    chosen, missing = select_tests(package, tests)
    report.problems.extend(missing)
    checked: list[_Checked] = []
    if workspace_id is not None:
        # The run reads the roles and calendars of the workspace as its processes would.
        await authorize(ctx, Permission.PROCESSES_READ, resource=process_scope(workspace_id))

    async with transaction(session_factory) as db:
        # The whole run reads: PostgreSQL refuses any write of this transaction.
        await db.execute(text("SET TRANSACTION READ ONLY"))
        with WriteGuard(db) as guard:
            if workspace_id is not None and not await workspace_exists(db, ctx, workspace_id):
                raise NotFoundError(
                    "Workspace not found", details={"workspaceId": str(workspace_id)}
                )
            calendars: dict[str, Calendar] = {}
            for obj in package.of_kind("Calendar"):
                calendar, problems = check_calendar(obj)
                report.problems.extend(obj.place(p) for p in problems)
                if calendar is not None:
                    calendars[obj.key] = calendar
            for obj in package.of_kind("Process"):
                done, problems = await _check_process(
                    db, ctx, settings, obj, package, calendars, workspace_id
                )
                report.problems.extend(problems)
                if done is not None:
                    checked.append(done)
            report.problems.extend(_test_problems(package, checked))
            if not check_only and not any(p.error for p in report.problems):
                given = {str((t.data.get("given") or {}).get("calendar") or "") for t in chosen}
                world = await _world(
                    db, ctx, package, checked, calendars, given - {""}, workspace_id
                )
                live = await _live_instances(db, ctx, chosen)
                world = replace(world, live=live, writes=lambda: guard.count)
                report.tests = [sandbox.run_test(world, t.file, t.data) for t in chosen]
                report.coverage = sandbox.package_coverage(
                    [c.definition for c in checked if c.definition is not None], report.tests
                )

    for item in checked:
        if item.scope is None:
            continue
        found = await governed_by_problems(
            provider, item.scope, item.spec, settings, trace_run_id=ctx.trace_run_id
        )
        report.problems.extend(item.obj.place(problem) for problem in found)
    report.problems.sort(key=lambda p: (not p.error, p.file or "", p.line or 0, p.path, p.code))
    report.duration_ms = int((time.monotonic() - started) * 1000)
    return report


async def _check_process(
    db: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    obj: PackageObject,
    package: ParsedPackage,
    calendars: Mapping[str, Calendar],
    workspace_id: uuid.UUID | None,
) -> tuple[_Checked | None, list[Problem]]:
    try:
        spec = with_workspace(normalized_spec(obj.spec), workspace_id)
    except SpecError as exc:
        return None, [obj.place(Problem("invalid_document", "error", exc.path, exc.message))]
    previous = await previous_version(db, ctx.tenant_id, obj.key, spec.get("version"))
    catalog = overlay_catalog(
        await load_catalog(db, ctx.tenant_id, obj.key, spec, previous),
        package,
        frozenset(calendars),
    )
    result = check_process(obj.key, spec, catalog, file=obj.file, locate=obj.locate)
    item = _Checked(obj, spec, catalog)
    if not result.errors:
        item.definition = engine.Definition.build(obj.key, spec, catalog)
    if governed_references(spec):
        item.scope = await regulation_scope(db, ctx, settings, spec)
    return item, list(result.problems)


def _test_problems(package: ParsedPackage, checked: Sequence[_Checked]) -> list[Problem]:
    versions = {c.obj.key: c.spec.get("version") for c in checked}
    problems = []
    for test in package.tests:
        wanted = test.data.get("version")
        if wanted is not None and test.process in versions and wanted != versions[test.process]:
            problems.append(
                Problem(
                    "test_version_mismatch",
                    "error",
                    "/version",
                    f"the test is for version {wanted}, the package has "
                    f"version {versions[test.process]} of {test.process}",
                    file=test.file,
                    line=test.locate("/version"),
                )
            )
    return problems


async def _live_instances(
    db: AsyncSession, ctx: AuthContext, chosen: Sequence[PackageTestFile]
) -> dict[str, sandbox.LiveInstance]:
    """The instances the trial runs start from (``given.fromInstance``), as the sandbox reads them.

    Each is read as ``GET /process-instances/{id}`` reads it: ``processes.read``
    on its workspace, an unknown or malformed id is 404.
    """
    live: dict[str, sandbox.LiveInstance] = {}
    for test in chosen:
        wanted = (test.data.get("given") or {}).get("fromInstance")
        if not wanted or str(wanted) in live:
            continue
        try:
            instance_id = uuid.UUID(str(wanted))
        except ValueError as exc:
            raise NotFoundError(
                "Process instance not found",
                details={"instanceId": str(wanted), "file": test.file},
            ) from exc
        instance = await get_instance(db, ctx, instance_id)
        refs = instance.refs or {}
        approval_refs = {
            uuid.UUID(ref.partition(":")[2]): target
            for ref, target in refs.items()
            if ref.startswith("approval:")
        }
        approvals: list[dict[str, Any]] = []
        if approval_refs:
            rows = await db.scalars(
                select(Approval)
                .where(Approval.id.in_(approval_refs), Approval.status == ApprovalStatus.PENDING)
                .order_by(Approval.created_at, Approval.id)
            )
            for row in rows:
                approver = str(row.assigned_principal_id)
                if row.required_role_id is not None:
                    role = await db.get(Role, row.required_role_id)
                    approver = f"role:{role.slug if role is not None else row.required_role_id}"
                target = approval_refs[row.id]
                approvals.append(
                    {
                        "activity": target.get("activity"),
                        "element": target.get("element"),
                        "approver": approver,
                        "excluded": list(row.excluded_principals or ()),
                    }
                )
        records = {
            ref.partition(":")[2]: target
            for ref, target in refs.items()
            if ref.startswith("activity:")
        }
        live[str(wanted)] = sandbox.LiveInstance(
            id=str(instance.id),
            process=instance.definition_key,
            state=instance.state or {},
            approvals=tuple(approvals),
            pending={k: list(r.get("pending") or ()) for k, r in records.items()},
            totals={k: int(r["total"]) for k, r in records.items() if r.get("total") is not None},
        )
    return live


async def _world(
    db: AsyncSession,
    ctx: AuthContext,
    package: ParsedPackage,
    checked: Sequence[_Checked],
    calendars: Mapping[str, Calendar],
    named_calendars: set[str],
    workspace_id: uuid.UUID | None,
) -> sandbox.World:
    """What the sandbox reads: the package's processes and the catalog they name."""
    definitions = {c.obj.key: c.definition for c in checked if c.definition is not None}
    named: set[str] = set()
    wanted_calendars: set[str] = set(named_calendars)
    skills: dict[str, SkillEntry] = {}
    task_types: dict[str, Any] = {}
    agents: set[str] = set()
    for item in checked:
        refs = references(item.spec)
        named |= refs.processes
        wanted_calendars |= refs.calendars
        skills.update(item.catalog.skills)
        task_types.update(item.catalog.task_types)
        agents |= item.catalog.agents
    nested = await _catalog_processes(db, ctx.tenant_id, named - set(definitions))
    for definition in nested.values():
        refs = references(definition.spec)
        wanted_calendars |= refs.calendars
        catalog = await load_catalog(db, ctx.tenant_id, definition.key, dict(definition.spec), None)
        skills = {**catalog.skills, **skills}
        task_types = {**catalog.task_types, **task_types}
        agents |= catalog.agents
    all_calendars = {
        **await _latest_calendars(db, ctx.tenant_id, wanted_calendars - set(calendars)),
        **calendars,
    }
    roles = await _role_slugs(db, ctx.tenant_id, workspace_id)
    return sandbox.World(
        definitions={**nested, **definitions},
        skills=skills,
        task_types=task_types,
        agents=frozenset(agents),
        roles=roles | {obj.key for obj in package.of_kind("Role")},
        calendars=all_calendars,
    )
