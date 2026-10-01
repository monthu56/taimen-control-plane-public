"""Moving an open instance to another version by a migration map (CP-ADR-0074 §11).

A process version declares ``migrations: [{from, to, policy, map}]``: the
instances of version ``from`` either finish where they are (``pin``) or move
to version ``to`` (``migrate``), their state carried over by ``map`` — old
element id → new element id; an id the map does not name keeps its id.

- :func:`standing` — the elements an open instance stands on: the stages it
  is in, the steps its threads wait on or execute inside, its timers, the
  steps whose compensation it still owes. A version that loses one of them
  cannot continue the instance: without a migration that maps it the plan
  says ``migration_required``.
- :func:`migrate_state` — the state of the instance on the new version: every
  element id renamed by the map, every position of a thread moved to the
  same element in the new block (a position is "after this element", not an
  index into a list that changed), the expression pointers of timers moved
  with their elements, stages the new version adds are ``available``. What
  cannot be carried — an element gone, an element of another kind, a block
  or an expression the new version does not have — is a
  :class:`MigrationError` naming the elements.

The engine never sees the move itself: the migrated state is a state of the
new version as if the instance had run on it. The instance's journal records
the migration with the migrated state whole (:func:`migration_record`), and a
replay starts from it (:mod:`control_plane.domain.process_replay`). The
record carries the engine revision of the target version: from it on the
instance runs under that revision (CP-ADR-0074, amendment 2026-09-29). Under
a revision with SLA deadlines the next input is the engine's own
``migrated`` (:data:`RECOUNT_INPUT`): the deadlines, escalations and ``onDue``
of the open steps and of the process are counted again by the new version;
until then their timers keep the old moments, so :func:`migrate_state` does
not refuse an expression of a due the new version dropped.

Pure functions over plain values; no I/O.
"""

import copy
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from control_plane.domain.process_definition import element_kinds, pointer, step_kind
from control_plane.domain.process_engine import SLA_REVISION, Definition
from control_plane.domain.process_sla import PROCESS, SLA_TIMERS

# The kind of the journal entry of a migration; its body holds the migrated state.
MIGRATION_INPUT = "migrate"
# The engine input right after it that recounts the deadlines by the new version.
RECOUNT_INPUT = "migrated"
PIN = "pin"
MIGRATE = "migrate"
POLICIES = (PIN, MIGRATE)
# An element that is no element of the spec: the review of a closed case.
_RETROSPECTIVE = "retrospective"
# Block names are "<prefix>:<element id>[/<rest>]" (process_engine.Definition).
_ELEMENT_BLOCKS = ("stage", "step", "branch", "timer")
_FRESH_STAGE = {"state": "available", "enteredAt": None, "runs": 0, "closedSeq": None}
# Timers the engine counts again by the new version after a migration (input
# ``migrated``, CP-ADR-0074 §11 amendment): deadlines, escalations, ``onDue``.
_RECOUNTED = (*SLA_TIMERS, "escalation", "due")


def _of_process(timer: Mapping[str, Any]) -> bool:
    """A deadline timer of the process itself: its element names no element of the version."""
    return timer.get("sla") == PROCESS


class MigrationError(ValueError):
    """The state cannot be carried to the new version; ``elements`` are old element ids."""

    def __init__(self, code: str, message: str, elements: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.elements = tuple(sorted(set(elements)))


Rename = Callable[[str], str]


def renamer(mapping: Mapping[str, str] | None) -> Rename:
    names = dict(mapping or {})
    return lambda element: names.get(element, element)


def kinds(definition: Definition) -> dict[str, str]:
    """Element ids of a version and their kinds; a step's kind names its step kind."""
    found = element_kinds(definition.spec)
    for sid, entry in definition.steps.items():
        found[sid] = f"step:{step_kind(entry.node)}"
    return found


# --- what an instance stands on ----------------------------------------------------------


def block_element(block: str) -> str | None:
    """The element a block belongs to: ``stage:a/steps`` → ``a``; ``correlate:0`` → none."""
    prefix, sep, rest = block.partition(":")
    if not sep or prefix not in _ELEMENT_BLOCKS:
        return None
    return rest.split("/", 1)[0]


def _rename_block(block: str, rename: Rename) -> str:
    element = block_element(block)
    if element is None:
        return block
    prefix, _, rest = block.partition(":")
    tail = rest[len(element) :]
    return f"{prefix}:{rename(element)}{tail}"


def _scope_element(scope: Any) -> str | None:
    if isinstance(scope, str) and scope.startswith("stage:"):
        return scope[len("stage:") :]
    return None


def _anchor(definition: Definition, frame: Mapping[str, Any]) -> str | None:
    """The element a sequence frame is at: the item it executed last (the index is past it)."""
    index = int(frame.get("index") or 0)
    if index <= 0:
        return None
    block = definition.blocks.get(str(frame["block"]))
    if block is None:
        return None
    items = block[1]
    if not items:
        return None
    item = items[min(index, len(items)) - 1]
    return str(item["id"]) if isinstance(item, Mapping) and "id" in item else None


def standing(definition: Definition, state: Mapping[str, Any]) -> set[str]:
    """The elements of ``definition`` an open instance stands on (see the module docstring)."""
    found: set[str] = set()
    for sid, record in (state.get("stages") or {}).items():
        if record.get("state") == "active":
            found.add(sid)
    for activity in (state.get("activities") or {}).values():
        element = activity.get("element")
        if element and element != _RETROSPECTIVE:
            found.add(str(element))
    for thread in (state.get("threads") or {}).values():
        scope = _scope_element(thread.get("scope"))
        if scope:
            found.add(scope)
        for frame in thread.get("stack") or ():
            kind = frame.get("kind")
            if kind == "seq":
                for element in (block_element(str(frame["block"])), _anchor(definition, frame)):
                    if element:
                        found.add(element)
            elif kind in ("try", "fork"):
                found.add(str(frame["step"]))
                found.update(str(b) for b in frame.get("branches") or ())
            elif kind == "compensation":
                found.update(str(s) for s in frame.get("steps") or ())
    for timer in (state.get("timers") or {}).values():
        if timer.get("state") in ("pending", "frozen"):
            if timer.get("element") and not _of_process(timer):
                found.add(str(timer["element"]))
            scope = _scope_element(timer.get("scope"))
            if scope:
                found.add(scope)
    for done in state.get("done") or ():
        if not done.get("compensated"):
            found.add(str(done["step"]))
    return found


def uncovered(
    definition: Definition,
    state: Mapping[str, Any],
    target: Definition,
    mapping: Mapping[str, str] | None = None,
) -> list[str]:
    """Elements the instance stands on that ``target`` lacks after the map, or has otherwise."""
    rename = renamer(mapping)
    before, after = kinds(definition), kinds(target)
    return sorted(
        element
        for element in standing(definition, state)
        if after.get(rename(element)) is None
        or (element in before and before[element] != after[rename(element)])
    )


# --- carrying the state ------------------------------------------------------------------


def _element_paths(definition: Definition) -> list[tuple[str, str]]:
    """``(path, element)`` of every element a pointer may start with, longest first."""
    paths = [(entry.path, sid) for sid, entry in definition.steps.items()]
    paths += [(timer.path, tid) for tid, timer in definition.timers.items()]
    paths += [
        (pointer("spec", "stages", index), str(stage["id"]))
        for index, stage in enumerate(definition.spec.get("stages") or ())
    ]
    return sorted(paths, key=lambda item: len(item[0]), reverse=True)


class _Carrier:
    def __init__(self, old: Definition, new: Definition, rename: Rename) -> None:
        self.old = old
        self.new = new
        self.rename = rename
        self.old_paths = _element_paths(old)
        self.new_paths = {element: path for path, element in _element_paths(new)}

    def repoint(self, path: str) -> str:
        """An expression pointer of the old version, moved with its element."""
        for prefix, element in self.old_paths:
            if path == prefix or path.startswith(prefix + "/"):
                target = self.new_paths.get(self.rename(element))
                if target is None:
                    raise MigrationError(
                        "element_gone",
                        f"{element!r} has no place in version {self.new.version}",
                        [element],
                    )
                return target + path[len(prefix) :]
        return path

    def recipe(self, recipe: Any, element: str, *, recounted: bool = False) -> Any:
        """A timer's recipe with its pointers moved.

        A ``recounted`` timer is counted again by the new version right after
        the migration: an expression the new version dropped with its due is
        no reason to refuse it.
        """
        if not isinstance(recipe, dict):
            return recipe
        out = dict(recipe)
        if isinstance(out.get("path"), str):
            out["path"] = self.repoint(out["path"])
            if out.get("kind") == "at" and out["path"] not in self.new.programs and not recounted:
                raise MigrationError(
                    "expression_gone",
                    f"version {self.new.version} has no expression at {out['path']}"
                    f" for the timer of {element!r}",
                    [element],
                )
        for name in ("due", "after"):
            if isinstance(out.get(name), dict):
                out[name] = self.recipe(out[name], element, recounted=recounted)
        return out

    def frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("kind")
        rename = self.rename
        if kind == "seq":
            old_block = str(frame["block"])
            new_block = _rename_block(old_block, rename)
            owner = block_element(old_block)
            if new_block not in self.new.blocks:
                raise MigrationError(
                    "block_gone",
                    f"version {self.new.version} has no block {new_block}",
                    [owner] if owner else [],
                )
            anchor = _anchor(self.old, frame)
            if anchor is not None:
                ids = [str(item.get("id")) for item in self.new.blocks[new_block][1]]
                if rename(anchor) not in ids:
                    raise MigrationError(
                        "element_moved",
                        f"{anchor!r} is not in block {new_block} of version {self.new.version}",
                        [anchor],
                    )
                frame["index"] = ids.index(rename(anchor)) + 1
            frame["block"] = new_block
        elif kind in ("try", "fork"):
            frame["step"] = rename(str(frame["step"]))
            if kind == "fork":
                frame["branches"] = [rename(str(b)) for b in frame.get("branches") or ()]
                frame["finished"] = [rename(str(b)) for b in frame.get("finished") or ()]
        elif kind == "compensation":
            frame["steps"] = [rename(str(s)) for s in frame.get("steps") or ()]
            if frame.get("element"):
                frame["element"] = rename(str(frame["element"]))
            if frame.get("scope") not in (None, "all"):
                frame["scope"] = ",".join(rename(s) for s in str(frame["scope"]).split(","))

    def scope(self, scope: Any) -> Any:
        element = _scope_element(scope)
        return f"stage:{self.rename(element)}" if element else scope


def migrate_state(
    old: Definition,
    new: Definition,
    state: Mapping[str, Any],
    mapping: Mapping[str, str] | None,
) -> dict[str, Any]:
    """The state of an open instance of ``old`` carried to ``new`` by ``mapping``.

    ``seq`` is not advanced here: the caller records the migration as the
    next journal entry.
    """
    rename = renamer(mapping)
    missing = uncovered(old, state, new, mapping)
    if missing:
        raise MigrationError(
            "migration_required",
            f"version {new.version} has no element for {', '.join(missing)} the instance stands on",
            missing,
        )
    carrier = _Carrier(old, new, rename)
    recounted = recounts(new)
    out = copy.deepcopy(dict(state))

    stages = {rename(sid): record for sid, record in (state.get("stages") or {}).items()}
    out["stages"] = {sid: copy.deepcopy(stages.get(sid) or _FRESH_STAGE) for sid in new.stage_ids}
    milestones = {m for m, kind in element_kinds(new.spec).items() if kind == "milestone"}
    out["milestones"] = {
        rename(m): value
        for m, value in (state.get("milestones") or {}).items()
        if rename(m) in milestones
    }
    for thread in out.get("threads", {}).values():
        thread["scope"] = carrier.scope(thread.get("scope"))
        for frame in thread.get("stack") or ():
            carrier.frame(frame)
    for activity in out.get("activities", {}).values():
        element = activity.get("element")
        if element and element != _RETROSPECTIVE:
            activity["element"] = rename(str(element))
    for timer in out.get("timers", {}).values():
        element = str(timer.get("element") or "")
        if timer.get("state") in ("pending", "frozen"):
            timer["recipe"] = carrier.recipe(
                timer.get("recipe"),
                element,
                recounted=recounted and timer.get("kind") in _RECOUNTED,
            )
        if element and not _of_process(timer):
            timer["element"] = rename(element)
        timer["scope"] = carrier.scope(timer.get("scope"))
    for done in out.get("done") or ():
        done["step"] = rename(str(done["step"]))
    for name in ("lastTask", "attention"):
        record = out.get(name)
        if isinstance(record, dict) and isinstance(record.get("element"), str):
            record["element"] = rename(record["element"])
    out["version"] = new.version
    out["definitionKey"] = new.key
    return out


def migration_record(
    *,
    from_key: str,
    from_version: int,
    target: Definition,
    mapping: Mapping[str, str],
    plan_hash: str,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The input body and the ``migrated`` decision of the journal entry of a migration.

    ``state`` is the migrated state (:func:`migrate_state`) with its ``seq``;
    both carry the version and the engine revision the instance moves to.
    """
    moved = {
        "fromKey": from_key,
        "fromVersion": from_version,
        "toVersion": target.version,
        "engineRevision": target.engine_revision,
        "policy": MIGRATE,
        "map": dict(mapping),
    }
    body = {**moved, "planHash": plan_hash, "state": dict(state)}
    return body, {"kind": "migrated", "element": None, **moved}


def recounts(target: Definition) -> bool:
    """Whether a migration to ``target`` is followed by the input ``migrated``."""
    return target.engine_revision >= SLA_REVISION


def recount_body(*, from_version: int, target: Definition) -> dict[str, Any]:
    """The body of the input ``migrated``: which move it follows, for the journal's reader."""
    return {"fromVersion": from_version, "toVersion": target.version}


def migration_for(spec: Mapping[str, Any], version: int) -> tuple[int, Mapping[str, Any]] | None:
    """The migration of ``spec`` for instances of ``version`` into ``spec``'s own version.

    Only migrations whose ``to`` is the version being published move
    instances now; others record what an earlier version did.
    """
    target = spec.get("version")
    for index, migration in enumerate(spec.get("migrations") or ()):
        if migration.get("from") == version and migration.get("to") == target:
            return index, migration
    return None
