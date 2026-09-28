"""Package plan and apply by hash: ``POST /packages:plan``, ``POST /packages:apply``.

CP-ADR-0074 §11, process-packages P015. The body is the package as its files,
as for ``packages:test``; the core plans the kinds whose versions and
instances it knows — ``Calendar`` and ``Process`` — and the installer applies
the rest.

**Plan** (:func:`plan_package`, a ``READ ONLY`` transaction; memory is asked
after it closes):

- ``changes`` — per object ``create | update | rename | unchanged`` and the
  fields it changes, each with its owner: ``console`` when a person changed
  it since the last apply (``package_objects`` keeps what that apply wanted),
  kept unless ``overwriteConsole`` (:func:`package_plan.diff_fields`);
- ``processes`` — per changed process the replay of the new version on the
  latest ``replayLimit`` instances of the current one, and the fate of open
  instances by version: ``pin`` or ``migrate`` by the version's
  ``migrations``, ``unaffected`` without one; ``migrationRequired`` when an
  element they stand on is gone and no migration carries them — a problem
  ``migration_required`` of the plan;
- ``regulationCoverage`` — the sections of each regulation the processes name
  (``governedBy``), as memory holds them, with the elements governed by each
  and the sections no element covers (FR-058);
- ``catalogEtag`` and ``planHash`` (:mod:`control_plane.domain.package_plan`).

**Apply** (:func:`apply_package`) builds the plan again from the same files in
its own transaction, under a lock of the tenant's applies and with the open
instances locked, and refuses ``409 plan_stale`` when its hash differs from
the one shown: the catalog or the instances changed since. A plan with
``migration_required`` is ``422 migration_required``, any other error ``422
invalid_package``. Then, in one transaction: calendars, then processes, are
published by the ordinary commands under the right of their kind; open
instances with ``migrate`` move to the new version by the map
(:func:`control_plane.domain.process_migration.migrate_state`) — a journal
entry ``migrate`` with the migrated state and ``process.migrated`` each; a key
renamed away is retired; ``package_objects`` records what the apply wanted.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.calendars import check_calendar_spec, publish_calendar
from control_plane.application.commands.package_test import (
    overlay_catalog,
    with_workspace,
    workspace_exists,
)
from control_plane.application.commands.process_definitions import (
    load_catalog,
    process_scope,
    publish_process_definition,
)
from control_plane.application.commands.process_instances import (
    CORRELATION_PREFIX,
    definition_of,
    engine_time,
)
from control_plane.application.commands.process_replays import chosen_instances, replay_one
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.context.graph import GraphScope
from control_plane.application.events import record_event
from control_plane.application.queries.process_regulations import (
    UNKNOWN_SECTION,
    document_sections,
    regulation_scope,
)
from control_plane.config import Settings
from control_plane.domain import process_engine as engine
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ConflictError, DomainError, NotFoundError, ValidationError
from control_plane.domain.package_plan import (
    PLANNED_KINDS,
    FieldChange,
    Rename,
    catalog_etag,
    diff_fields,
    package_hash,
    plan_hash,
    renames,
)
from control_plane.domain.package_source import PackageObject, ParsedPackage, parse_package
from control_plane.domain.process_definition import (
    GOVERNED_BY_UNCHECKED,
    Catalog,
    Problem,
    SpecError,
    check_process,
    definition_hash,
    governed_references,
    normalized_spec,
)
from control_plane.domain.process_migration import (
    MIGRATE,
    MIGRATION_INPUT,
    PIN,
    MigrationError,
    migrate_state,
    migration_for,
    renamer,
    uncovered,
)
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import (
    CalendarVersion,
    ProcessDefinition,
    ProcessInstance,
    ProcessInstanceEvent,
    ProcessRecall,
    ProcessTimer,
)
from control_plane.infrastructure.db.models import PackageObject as PackageRecord

# The spec of a package object of kind Calendar as ``POST /calendars`` takes
# it (its shape checked), or the findings of its shape.
CalendarSpec = Callable[[PackageObject], tuple[dict[str, Any] | None, list[Problem]]]
# Diverged instances named per process in the plan.
MAX_DIVERGED_IDS = 20
_OPEN = (engine.RUNNING, engine.SUSPENDED)


# --- the plan --------------------------------------------------------------------------------


@dataclass
class _Latest:
    """The latest version of a key in the catalog."""

    version: int
    hash: str
    spec: dict[str, Any]
    row: ProcessDefinition | CalendarVersion


@dataclass
class _Planned:
    """One object the plan applies."""

    kind: str
    key: str
    obj: PackageObject
    wanted: dict[str, Any]
    wanted_hash: str
    renamed_from: str | None = None
    latest: _Latest | None = None
    # What the last apply wanted of this key, and of the key it is renamed from.
    record: PackageRecord | None = None
    source_record: PackageRecord | None = None
    action: str = "create"
    fields: list[FieldChange] = field(default_factory=list)
    published: dict[str, Any] = field(default_factory=dict)
    version: int | None = None
    definition: engine.Definition | None = None

    @property
    def source(self) -> str:
        """The key whose instances and versions the object carries on."""
        return self.renamed_from or self.key

    def change(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "action": self.action,
            "renamedFrom": self.renamed_from,
            "fields": [item.out() for item in self.fields],
        }


@dataclass
class _Group:
    """The open instances of one version of a changed process."""

    process: _Planned
    version: int
    row: ProcessDefinition
    instances: list[ProcessInstance]
    fate: str
    migration: Mapping[str, Any] | None
    failures: list[tuple[ProcessInstance, MigrationError]] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "open": len(self.instances),
            "fate": self.fate,
            "migrationRequired": bool(self.failures),
        }


@dataclass
class PackagePlan:
    """``PackagePlanOut``."""

    package_key: str | None
    package_version: str | None
    package_hash: str
    overwrite: bool
    problems: list[Problem] = field(default_factory=list)
    planned: list[_Planned] = field(default_factory=list)
    groups: list[_Group] = field(default_factory=list)
    effective_renames: list[_Planned] = field(default_factory=list)
    behaviour: dict[str, dict[str, Any]] = field(default_factory=dict)
    coverage: list[dict[str, Any]] = field(default_factory=list)
    etag_entries: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def catalog_etag(self) -> str:
        return catalog_etag(self.etag_entries)

    def changes(self) -> list[dict[str, Any]]:
        return [item.change() for item in self.planned]

    def processes(self) -> list[dict[str, Any]]:
        out = []
        for item in self.planned:
            if item.kind != "Process" or item.action == "unchanged":
                continue
            out.append(
                {
                    "key": item.key,
                    "fromVersion": item.latest.version if item.latest else None,
                    "toVersion": item.published.get("version"),
                    "behaviour": self.behaviour.get(item.key),
                    "instances": [g.out() for g in self.groups if g.process is item],
                }
            )
        return out

    @property
    def hash(self) -> str:
        return plan_hash(
            self.package_hash,
            self.catalog_etag,
            self.changes(),
            self.processes(),
            overwrite=self.overwrite,
        )

    def out(self) -> dict[str, Any]:
        return {
            "planHash": self.hash,
            "catalogEtag": self.catalog_etag,
            "package": {"key": self.package_key, "version": self.package_version},
            "changes": self.changes(),
            "processes": self.processes(),
            "regulationCoverage": self.coverage,
            "problems": [p.out() for p in _sorted(self.problems)],
            "createdAt": self.created_at.isoformat(),
        }


def _sorted(problems: Sequence[Problem]) -> list[Problem]:
    return sorted(problems, key=lambda p: (not p.error, p.file or "", p.line or 0, p.path, p.code))


# --- reading the catalog ---------------------------------------------------------------------


async def _latest(db: AsyncSession, tenant_id: uuid.UUID, kind: str, key: str) -> _Latest | None:
    if kind == "Process":
        row = await db.scalar(
            select(ProcessDefinition)
            .where(ProcessDefinition.tenant_id == tenant_id, ProcessDefinition.key == key)
            .order_by(ProcessDefinition.version.desc())
            .limit(1)
        )
        return _Latest(row.version, row.definition_hash, row.spec, row) if row else None
    calendar = await db.scalar(
        select(CalendarVersion)
        .where(CalendarVersion.tenant_id == tenant_id, CalendarVersion.key == key)
        .order_by(CalendarVersion.version.desc())
        .limit(1)
    )
    if calendar is None:
        return None
    return _Latest(calendar.version, calendar.calendar_hash, calendar.spec, calendar)


async def _records(
    db: AsyncSession, tenant_id: uuid.UUID, pairs: set[tuple[str, str]], *, lock: bool
) -> dict[tuple[str, str], PackageRecord]:
    if not pairs:
        return {}
    stmt = select(PackageRecord).where(
        PackageRecord.tenant_id == tenant_id,
        tuple_(PackageRecord.kind, PackageRecord.key).in_(sorted(pairs)),
    )
    if lock:
        stmt = stmt.with_for_update()
    return {(r.kind, r.key): r for r in await db.scalars(stmt)}


async def catalog_entries(
    db: AsyncSession, tenant_id: uuid.UUID, pairs: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """What the catalog etag is computed from, for the objects ``pairs``."""
    records = await _records(db, tenant_id, pairs, lock=False)
    entries = []
    for kind, key in sorted(pairs):
        latest = await _latest(db, tenant_id, kind, key)
        record = records.get((kind, key))
        entries.append(
            {
                "kind": kind,
                "key": key,
                "version": latest.version if latest else None,
                "hash": latest.hash if latest else None,
                "applied": record.spec_hash if record else None,
                "retired": record is not None and record.retired_at is not None,
            }
        )
    return entries


# --- building the plan -----------------------------------------------------------------------


async def build_plan(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    files: Sequence[tuple[str, str]],
    workspace_id: uuid.UUID | None,
    overwrite: bool,
    calendar_spec: CalendarSpec,
    lock: bool = False,
) -> PackagePlan:
    """The plan without its reports (behaviour, coverage): what the hash covers."""
    package = parse_package(files)
    manifest = package.manifest_object
    plan = PackagePlan(
        package_key=manifest.key if manifest else None,
        package_version=str(manifest.spec.get("version")) if manifest else None,
        package_hash=package_hash(files),
        overwrite=overwrite,
    )
    plan.problems.extend(package.problems)
    if manifest is None:
        plan.problems.append(
            Problem(
                "package_manifest_missing",
                "error",
                "",
                "the package has no package.yaml: an apply records its objects under the package",
                file="package.yaml",
            )
        )
    listed, found = renames(package)
    plan.problems.extend(found)
    by_target = {(r.kind, r.target): r for r in listed}
    pairs = {(o.kind, o.key) for o in package.objects if o.kind in PLANNED_KINDS}
    pairs |= {(r.kind, r.source) for r in listed}
    records = await _records(db, ctx.tenant_id, pairs, lock=lock)
    plan.etag_entries = await catalog_entries(db, ctx.tenant_id, pairs)

    calendars = frozenset(o.key for o in package.of_kind("Calendar"))
    for obj in sorted(package.of_kind("Calendar"), key=lambda o: o.key):
        sent, problems = calendar_spec(obj)
        plan.problems.extend(obj.place(p) for p in problems)
        if sent is None:
            continue
        body, body_hash, _ = check_calendar_spec(sent)
        item = _Planned("Calendar", obj.key, obj, body, body_hash)
        await _place(db, ctx, plan, item, by_target, records, manifest)
        plan.planned.append(item)
    for obj in sorted(package.of_kind("Process"), key=lambda o: o.key):
        try:
            body = normalized_spec(obj.spec)
        except SpecError as exc:
            plan.problems.append(
                obj.place(Problem("invalid_document", "error", exc.path, exc.message))
            )
            continue
        if workspace_id is not None:
            body = with_workspace(body, workspace_id)
        item = _Planned("Process", obj.key, obj, body, definition_hash(body))
        await _place(db, ctx, plan, item, by_target, records, manifest)
        await _check(db, ctx, plan, item, package, calendars)
        plan.planned.append(item)
        if item.action in ("update", "rename"):
            await _instances(db, ctx, plan, item, lock=lock)
    return plan


async def _place(
    db: AsyncSession,
    ctx: AuthContext,
    plan: PackagePlan,
    item: _Planned,
    by_target: Mapping[tuple[str, str], Rename],
    records: Mapping[tuple[str, str], PackageRecord],
    manifest: PackageObject | None,
) -> None:
    """Where the object comes from (its key or a key it is renamed from) and what changes."""
    latest = await _latest(db, ctx.tenant_id, item.kind, item.key)
    item.record = records.get((item.kind, item.key))
    base = item.record if item.record is not None and item.record.retired_at is None else None
    rename = by_target.get((item.kind, item.key))
    if rename is not None:
        source = await _latest(db, ctx.tenant_id, item.kind, rename.source)
        record = records.get((item.kind, rename.source))
        retired = record is not None and record.retired_at is not None
        if source is not None and not retired and latest is None:
            # The object moves with its versions and what its last apply wanted.
            item.renamed_from, item.source_record = rename.source, record
            latest, base = source, record
            plan.effective_renames.append(item)
        elif source is not None and not retired and manifest is not None:
            plan.problems.append(
                manifest.place(
                    Problem(
                        "rename_target_exists",
                        "error",
                        "/spec/renames",
                        f"{item.kind}/{rename.source} and {item.kind}/{item.key} both exist:"
                        " the rename has nothing to move to",
                        hint=f"retire {item.kind}/{rename.source} or drop the rename",
                    )
                )
            )
    item.latest = latest
    item.fields, item.published = diff_fields(
        latest.spec if latest else None,
        item.wanted,
        base.spec if base is not None else None,
        overwrite=plan.overwrite,
    )
    if item.kind == "Calendar":
        # Kept console fields join the package's: the stored form again.
        item.published, _, _ = check_calendar_spec(item.published)
    if latest is None:
        item.action = "create"
    elif item.renamed_from is not None:
        item.action = "rename"
    elif item.published == latest.spec:
        item.action = "unchanged"
    else:
        item.action = "update"
    if item.kind == "Calendar":
        if item.action == "unchanged" and latest is not None:
            item.version = latest.version
        elif latest is None or item.renamed_from is not None:
            item.version = 1
        else:
            item.version = latest.version + 1
        return
    version = item.published.get("version")
    item.version = latest.version if item.action == "unchanged" and latest else version
    if (
        item.action in ("update", "rename")
        and latest is not None
        and isinstance(version, int)
        and version <= latest.version
    ):
        plan.problems.append(
            item.obj.place(
                Problem(
                    "process_version_conflict",
                    "error",
                    "/spec/version",
                    f"version {version} is not above the latest version {latest.version}"
                    f" of {item.source}",
                    hint=f"raise spec.version to {latest.version + 1}",
                )
            )
        )


async def _check(
    db: AsyncSession,
    ctx: AuthContext,
    plan: PackagePlan,
    item: _Planned,
    package: ParsedPackage,
    calendars: frozenset[str],
) -> None:
    """The check of a publication (CP-ADR-0074 §2) of the spec the apply would publish."""
    if item.action == "unchanged":
        return
    previous = item.latest.row if item.latest is not None else None
    catalog: Catalog = overlay_catalog(
        await load_catalog(
            db,
            ctx.tenant_id,
            item.key,
            item.published,
            previous,  # type: ignore[arg-type]
            history_key=item.renamed_from,
        ),
        package,
        calendars,
    )
    checked = check_process(
        item.key, item.published, catalog, file=item.obj.file, locate=item.obj.locate
    )
    plan.problems.extend(checked.problems)
    if not checked.errors:
        item.definition = engine.Definition.build(item.key, item.published, catalog)


async def _instances(
    db: AsyncSession, ctx: AuthContext, plan: PackagePlan, item: _Planned, *, lock: bool
) -> None:
    """The open instances of the key the process carries on, by version, and their fate."""
    stmt = (
        select(ProcessInstance)
        .where(
            ProcessInstance.tenant_id == ctx.tenant_id,
            ProcessInstance.definition_key == item.source,
            ProcessInstance.status.in_(_OPEN),
        )
        .order_by(ProcessInstance.definition_version, ProcessInstance.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    by_version: dict[int, list[ProcessInstance]] = {}
    for instance in await db.scalars(stmt):
        by_version.setdefault(instance.definition_version, []).append(instance)
    for version, instances in sorted(by_version.items()):
        row = await db.scalar(
            select(ProcessDefinition).where(
                ProcessDefinition.tenant_id == ctx.tenant_id,
                ProcessDefinition.key == item.source,
                ProcessDefinition.version == version,
            )
        )
        assert row is not None  # an instance is pinned to a published version
        found = migration_for(item.published, version)
        migration = found[1] if found else None
        policy = migration.get("policy") if migration else None
        fate = PIN if policy == PIN else MIGRATE if migration else "unaffected"
        group = _Group(item, version, row, instances, fate, migration)
        plan.groups.append(group)
        if fate == PIN or item.definition is None:
            continue
        try:
            old = await definition_of(db, row)
        except DomainError as exc:
            error = MigrationError("definition_unusable", exc.message)
            group.failures = [(instance, error) for instance in instances]
        else:
            mapping = (migration or {}).get("map") or {}
            for instance in instances:
                try:
                    if fate == MIGRATE:
                        migrate_state(old, item.definition, instance.state, mapping)
                    else:
                        missing = uncovered(old, instance.state, item.definition)
                        if missing:
                            raise MigrationError(
                                "migration_required",
                                f"version {item.definition.version} has no element for"
                                f" {', '.join(missing)}",
                                missing,
                            )
                except MigrationError as exc:
                    group.failures.append((instance, exc))
        if group.failures:
            plan.problems.append(_migration_required(item, group, found[0] if found else None))


def _migration_required(item: _Planned, group: _Group, index: int | None) -> Problem:
    elements = sorted({e for _, exc in group.failures for e in exc.elements})
    reasons = sorted({exc.message for _, exc in group.failures})
    target = item.published.get("version")
    where = f"/spec/migrations/{index}" if index is not None else "/spec"
    listed = f" on {', '.join(elements)}" if elements else ""
    hint = (
        "map every element the instances stand on to an element of the new version"
        if index is not None
        else f"add migrations: [{{from: {group.version}, to: {target}, policy: pin | migrate,"
        " map: {old element: new element}}]"
    )
    return item.obj.place(
        Problem(
            "migration_required",
            "error",
            where,
            f"{len(group.failures)} open instance(s) of {item.source}@{group.version} stand"
            f"{listed}; version {target} cannot carry them: {reasons[0]}",
            hint=hint,
        )
    )


# --- reports of the plan -----------------------------------------------------------------------


async def _behaviour(db: AsyncSession, ctx: AuthContext, plan: PackagePlan, limit: int) -> None:
    """The replay of each changed process on the latest instances of its current version."""
    for item in plan.planned:
        if item.definition is None or item.latest is None or item.action == "unchanged":
            continue
        instances = await chosen_instances(db, ctx, item.source, item.latest.version, None, limit)
        # A renamed process replays under the key its instances ran: the key is not behaviour.
        candidate = replace(item.definition, key=item.source)
        diverged: list[str] = []
        for instance in instances:
            result = await replay_one(db, candidate, instance)
            if result.divergence is not None:
                diverged.append(str(instance.id))
        plan.behaviour[item.key] = {
            "replayed": len(instances),
            "diverged": len(diverged),
            "instanceIds": diverged[:MAX_DIVERGED_IDS],
        }


@dataclass(frozen=True)
class _Reference:
    process: _Planned
    document: str
    section: str | None
    element: str
    path: str


def _references(item: _Planned) -> list[_Reference]:
    """Every ``governedBy`` of a process with the element it governs."""
    spec = item.published if item.action != "unchanged" else item.wanted
    places: list[tuple[str, str]] = []
    for index, stage in enumerate(spec.get("stages") or ()):
        places.append((f"/spec/stages/{index}", str(stage.get("id"))))
    if item.definition is not None:
        places += [(entry.path, sid) for sid, entry in item.definition.steps.items()]
    for index, table in enumerate(spec.get("decisions") or ()):
        places.append((f"/spec/decisions/{index}", str(table.get("id"))))
    for index, stage in enumerate(spec.get("stages") or ()):
        for number, milestone in enumerate(stage.get("milestones") or ()):
            places.append((f"/spec/stages/{index}/milestones/{number}", str(milestone.get("id"))))
    places.sort(key=lambda p: len(p[0]), reverse=True)
    found = []
    for document, path in governed_references(spec):
        at = path[: -len("/document")]
        node: Any = spec
        for part in at.split("/")[2:]:
            node = node[int(part)] if isinstance(node, list) else node.get(part)
        section = node.get("section") if isinstance(node, Mapping) else None
        element = next((element for prefix, element in places if at.startswith(prefix + "/")), None)
        label = f"{item.key}/{element}" if element else item.key
        found.append(
            _Reference(item, document, section if isinstance(section, str) else None, label, at)
        )
    return found


async def _coverage(
    provider: GraphProvider | None,
    scoped: Sequence[tuple[_Planned, GraphScope]],
    settings: Settings,
    trace_run_id: str,
) -> tuple[list[dict[str, Any]], list[Problem]]:
    """Coverage of the sections of each named regulation, and the problems it finds."""
    references = [(ref, scope) for item, scope in scoped for ref in _references(item)]
    if not references:
        return [], []
    sections: dict[str, list[str] | None] = {}
    reason: str | None = None
    if provider is None:
        reason = "memory is not configured"
    else:
        for item, scope in scoped:
            wanted = [r.document for r in _references(item) if r.document not in sections]
            if not wanted:
                continue
            try:
                sections.update(
                    await document_sections(
                        provider, scope, wanted, settings, trace_run_id=trace_run_id
                    )
                )
            except TimeoutError:
                reason = "memory did not answer in time"
                break
            except ContextProviderError:
                reason = "memory failed to answer"
                break
    if reason is not None:
        return [], [
            Problem(
                GOVERNED_BY_UNCHECKED,
                "warning",
                "",
                f"the coverage of the regulations was not computed: {reason}",
                hint="plan again when memory is available",
            )
        ]
    coverage: list[dict[str, Any]] = []
    problems: list[Problem] = []
    for document in dict.fromkeys(r.document for r, _ in references):
        known = sections.get(document)
        covered: dict[str, list[str]] = {}
        for ref, _ in references:
            if ref.document != document or ref.section is None:
                continue
            covered.setdefault(ref.section, [])
            if ref.element not in covered[ref.section]:
                covered[ref.section].append(ref.element)
            if known and ref.section not in known:
                problems.append(
                    ref.process.obj.place(
                        Problem(
                            UNKNOWN_SECTION,
                            "warning",
                            ref.path + "/section",
                            f"{document!r} has no section {ref.section!r} in the knowledge base",
                            hint=f"sections: {', '.join(known[:20])}",
                        )
                    )
                )
        coverage.append(
            {
                "document": document,
                "found": known is not None,
                "covered": {k: covered[k] for k in sorted(covered)},
                "uncovered": [s for s in known or () if s not in covered],
            }
        )
    return coverage, problems


# --- routes' work ------------------------------------------------------------------------------


async def plan_package(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: AuthContext,
    settings: Settings,
    provider: GraphProvider | None,
    *,
    files: Sequence[tuple[str, str]],
    workspace_id: uuid.UUID | None,
    replay_limit: int,
    overwrite: bool,
    calendar_spec: CalendarSpec,
) -> PackagePlan:
    """``POST /packages:plan``: nothing is written."""
    await authorize(ctx, Permission.PACKAGES_PLAN)
    if workspace_id is not None:
        await authorize(ctx, Permission.PROCESSES_READ, resource=process_scope(workspace_id))
    scoped: list[tuple[_Planned, GraphScope]] = []
    async with transaction(session_factory) as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        if workspace_id is not None and not await workspace_exists(db, ctx, workspace_id):
            raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
        plan = await build_plan(
            db,
            ctx,
            files=files,
            workspace_id=workspace_id,
            overwrite=overwrite,
            calendar_spec=calendar_spec,
        )
        if replay_limit > 0:
            await _behaviour(db, ctx, plan, replay_limit)
        for item in plan.planned:
            spec = item.published if item.action != "unchanged" else item.wanted
            if item.kind == "Process" and governed_references(spec):
                try:
                    scoped.append((item, await regulation_scope(db, ctx, settings, spec)))
                except ValueError:
                    continue  # a workspace id that is no UUID: the check has said so
    plan.coverage, found = await _coverage(provider, scoped, settings, ctx.trace_run_id)
    plan.problems.extend(found)
    return plan


async def _lock_applies(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """One apply of a tenant at a time: each plans on the catalog the previous one left."""
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:packages:{tenant_id}", 0)))
    )


async def apply_package(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    files: Sequence[tuple[str, str]],
    expected_hash: str,
    workspace_id: uuid.UUID | None,
    overwrite: bool,
    calendar_spec: CalendarSpec,
) -> dict[str, Any]:
    """``POST /packages:apply``: exactly the plan with ``expected_hash``, or a refusal."""
    await authorize(ctx, Permission.PACKAGES_PLAN)
    if workspace_id is not None:
        await authorize(ctx, Permission.PROCESSES_READ, resource=process_scope(workspace_id))
    await _lock_applies(db, ctx.tenant_id)
    plan = await build_plan(
        db,
        ctx,
        files=files,
        workspace_id=workspace_id,
        overwrite=overwrite,
        calendar_spec=calendar_spec,
        lock=True,
    )
    current = plan.hash
    if current != expected_hash:
        raise ConflictError(
            "plan_stale",
            "The catalog or the open instances changed since the plan was built:"
            " build the plan again and apply the new one",
            details={
                "planHash": expected_hash,
                "currentPlanHash": current,
                "catalogEtag": plan.catalog_etag,
            },
        )
    problems = _sorted(plan.problems)
    required = [p for p in problems if p.code == "migration_required"]
    if required:
        raise ValidationError(
            "migration_required",
            f"{required[0].message} (and {len(required) - 1} more)"
            if len(required) > 1
            else required[0].message,
            details={"problems": [p.out() for p in problems]},
        )
    errors = [p for p in problems if p.error]
    if errors:
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise ValidationError(
            "invalid_package",
            f"The package is invalid: {errors[0].message} at {errors[0].path}{more}",
            details={"problems": [p.out() for p in problems]},
        )
    published: dict[str, ProcessDefinition] = {}
    for item in plan.planned:
        if item.action == "unchanged":
            continue
        if item.kind == "Calendar":
            calendar = await publish_calendar(db, ctx, key=item.key, spec=item.published)
            item.version = calendar.row.version
        else:
            process = await publish_process_definition(
                db, ctx, key=item.key, spec=item.published, renamed_from=item.renamed_from
            )
            item.version = process.row.version
            published[item.key] = process.row
    for group in plan.groups:
        if group.fate != MIGRATE:
            continue
        target = published[group.process.key]
        for instance in group.instances:
            await _migrate(db, ctx, instance, group, target, current)
    now = utcnow()
    for item in plan.effective_renames:
        assert item.renamed_from is not None and item.latest is not None
        record = item.source_record or _new_record(
            ctx, plan, item.kind, item.renamed_from, item.latest.spec, current
        )
        db.add(record)
        record.version, record.spec_hash = item.latest.version, item.latest.hash
        record.retired_at = now
        _stamp(record, ctx, current, now)
    for item in plan.planned:
        record = item.record or _new_record(ctx, plan, item.kind, item.key, item.wanted, current)
        db.add(record)
        record.package_key = plan.package_key or record.package_key
        record.version = int(item.version or 0)
        record.spec, record.spec_hash = item.wanted, item.wanted_hash
        record.retired_at = None
        _stamp(record, ctx, current, now)
    pairs = {(e["kind"], e["key"]) for e in plan.etag_entries}
    await db.flush()
    return {
        "planHash": current,
        "catalogEtag": catalog_etag(await catalog_entries(db, ctx.tenant_id, pairs)),
        "applied": [
            {"kind": item.kind, "key": item.key, "action": item.action, "version": item.version}
            for item in plan.planned
        ],
    }


def _new_record(
    ctx: AuthContext, plan: PackagePlan, kind: str, key: str, spec: dict[str, Any], current: str
) -> PackageRecord:
    return PackageRecord(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        kind=kind,
        key=key,
        package_key=plan.package_key or "",
        version=0,
        spec_hash="",
        spec=spec,
        plan_hash=current,
        applied_by=ctx.principal_id,
        applied_at=utcnow(),
        retired_at=None,
    )


def _stamp(record: PackageRecord, ctx: AuthContext, current: str, now: datetime) -> None:
    record.plan_hash = current
    record.applied_by = ctx.principal_id
    record.applied_at = now


async def _migrate(
    db: AsyncSession,
    ctx: AuthContext,
    instance: ProcessInstance,
    group: _Group,
    target: ProcessDefinition,
    current: str,
) -> None:
    """Move one open instance to ``target`` by the group's map: journal entry and event."""
    old = await definition_of(db, group.row)
    new = await definition_of(db, target)
    mapping = dict((group.migration or {}).get("map") or {})
    try:
        state = migrate_state(old, new, instance.state, mapping)
    except MigrationError as exc:  # pragma: no cover - the plan was built under the same locks
        raise ValidationError(
            "migration_required", exc.message, details={"instanceId": str(instance.id)}
        ) from exc
    seq = int(instance.state["seq"]) + 1
    state["seq"] = seq
    at = engine_time(instance, utcnow())
    from_version = instance.definition_version
    body = {
        "fromKey": instance.definition_key,
        "fromVersion": from_version,
        "toVersion": target.version,
        "policy": MIGRATE,
        "map": mapping,
        "planHash": current,
        "state": state,
    }
    given = engine.Input(MIGRATION_INPUT, at, body, str(ctx.principal_id))
    db.add(
        ProcessInstanceEvent(
            instance_id=instance.id,
            seq=seq,
            tenant_id=instance.tenant_id,
            at=at,
            kind=MIGRATION_INPUT,
            source_ref=f"migration:{target.id}",
            event_id=None,
            actor_id=ctx.principal_id,
            input=given.out(),
            decisions=[
                {
                    "kind": "migrated",
                    "element": None,
                    "fromKey": instance.definition_key,
                    "fromVersion": from_version,
                    "toVersion": target.version,
                    "policy": MIGRATE,
                    "map": mapping,
                }
            ],
            intents=[],
            calendars={},
            created_at=utcnow(),
        )
    )
    rename = renamer(mapping)
    instance.definition_id = target.id
    instance.definition_key = target.key
    instance.definition_version = target.version
    instance.state = state
    instance.data = state.get("data") or {}
    instance.updated_at = utcnow()
    instance.refs = {
        ref: (
            {**value, "element": rename(value["element"])}
            if isinstance(value, dict) and isinstance(value.get("element"), str)
            else value
        )
        for ref, value in (instance.refs or {}).items()
    }
    for timer in await db.scalars(
        select(ProcessTimer).where(
            ProcessTimer.instance_id == instance.id,
            ProcessTimer.state.in_(("pending", "frozen")),
        )
    ):
        timer.element = rename(timer.element)
    for recall in await db.scalars(
        select(ProcessRecall).where(
            ProcessRecall.instance_id == instance.id, ProcessRecall.state == "pending"
        )
    ):
        recall.element = rename(recall.element)
    await db.flush()
    await record_event(
        db,
        tenant_id=instance.tenant_id,
        event_type="process.migrated",
        entity_type="process_instance",
        entity_id=instance.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=f"{CORRELATION_PREFIX}{instance.id}",
        trace_run_id=ctx.trace_run_id,
        payload={
            "instanceId": str(instance.id),
            "definitionKey": target.key,
            "version": target.version,
            "instanceKey": instance.instance_key,
            "fromVersion": from_version,
            "map": mapping,
            "policy": MIGRATE,
        },
    )
