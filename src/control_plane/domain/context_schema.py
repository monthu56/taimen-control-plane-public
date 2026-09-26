"""Context profile of a task type (TAI-ADR-0042 p.3-5, CP-ADR-0064).

A task type version may carry a ``context_schema``: which entities of the
tenant's knowledge graph a task of that type starts from, which relations the
Context Compiler follows from them, and at which moment. Core does not know
the domain — kinds and relations are names from the domain packs enabled for
the workspace namespace in Memory; this module only checks the shape, exactly
like ``approval_outcomes`` checks outcomes, when the version is published.

Document shape::

    {"anchors": [
        {"from": "description", "kinds": ["endpoint", "adr"]},
        {"from": "$.customFields.okpdCodes", "kind": "okpd_code"},
        {"from": "$.spawnedBy.artifact[commit].diffPaths", "kind": "source_file",
         "via": "defined_in"}],
     "traverse": [{"relation": "calls", "direction": "in", "depth": 1, "limit": 20},
                  {"relation": "governs", "direction": "in", "from": "previous"}],
     "asOf": "taskCreated",
     "budgetTokens": 4000}

``from`` is ``description`` / ``title`` (text: identifiers are extracted from
it with the ``idPatterns`` of the listed kinds) or a ``$.`` path in the grammar
of approval outcomes (``$.<field>``, ``$.customFields.<key>``,
``$.spawnedBy.<field>``, ``$.spawnedBy.artifact[<type>].<field>``): its value —
a string or a list of strings — is taken as anchor values as is. ``via`` names
a relation: the anchor is replaced by the entities that point at it over that
relation (``endpoint -defined_in-> source_file``: a changed file brings the
endpoints defined in it).

``asOf`` is ``taskCreated`` (default), ``now`` (pinned to the moment the claim
was taken, so a pack stays reproducible) or ``origin`` (the time of the
observation the task originated from; the task's creation when there is none).

Pure functions, no database and no I/O.
"""

import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import regex

from control_plane.domain.approval_outcomes import TASK_ROOTS, Path, parse_path
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import guard_json_document
from control_plane.domain.redaction import secret_material

INVALID = "invalid_context_schema"

AS_OF_TASK_CREATED = "taskCreated"
AS_OF_NOW = "now"
AS_OF_ORIGIN = "origin"
AS_OF_MODES = (AS_OF_TASK_CREATED, AS_OF_NOW, AS_OF_ORIGIN)

TEXT_SOURCES = frozenset({"description", "title"})
DIRECTIONS = ("in", "out", "both")
STEP_STARTS = ("anchors", "previous")

# Limits of Memory's typed traversal (platform_memory.context.typed): a schema
# that could never be compiled is refused at publication, not at claim.
MAX_ANCHOR_SPECS = 20
MAX_ANCHORS = 20
MAX_STEPS = 10
MAX_DEPTH = 5
MAX_STEP_LIMIT = 200
DEFAULT_STEP_LIMIT = 20
MAX_KINDS = 20
MAX_BUDGET_TOKENS = 32_000
# A pack without a declared budget is cut to the renderer's default share of
# the prompt (12000 characters, CONTROL_PLANE_CONTEXT_BUDGET_CHARS).
DEFAULT_BUDGET_TOKENS = 3_000
CHARS_PER_TOKEN = 4
MAX_SCHEMA_BYTES = 16 * 1024

# Text an identifier is extracted from, and one identifier, are bounded: pack
# patterns run in core, over tenant text.
MAX_TEXT_CHARS = 20_000
MAX_CANDIDATE_CHARS = 300
# Pack patterns are tenant data and may backtrack: matching stops after this
# many matches in all, and gives up (``TimeoutError``) after this many seconds.
# ``regex`` releases the GIL while matching, so a caller may run it in a thread.
MAX_MATCHES = 1_000
EXTRACTION_SECONDS = 1.0

# Kind and relation names as Memory declares them (MEM-ADR-020 p.1).
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ARTIFACT_SHORT = re.compile(r"(\.artifact\[[A-Za-z0-9_.:-]+\])\.(?!metadata\.)")
# Sentence punctuation glued to the end of an identifier ("see POST /runs.").
_TRAILING = ".,;:"


@dataclass(frozen=True)
class AnchorSpec:
    """Where anchor values of a task come from."""

    source: str
    path: Path
    kinds: tuple[str, ...] = ()
    via: str | None = None

    @property
    def textual(self) -> bool:
        """Free text to extract identifiers from, rather than values to take as is."""
        return self.path.field in TEXT_SOURCES and self.path.artifact_type is None


@dataclass(frozen=True)
class TraverseSpec:
    relation: str
    direction: str = "out"
    depth: int = 1
    limit: int = DEFAULT_STEP_LIMIT
    start: str = "anchors"

    def to_request(self) -> dict[str, Any]:
        """One ``traverse`` step of Memory's typed request."""
        return {
            "relation": self.relation,
            "direction": self.direction,
            "depth": self.depth,
            "limit": self.limit,
            "from": self.start,
        }


@dataclass(frozen=True)
class ContextSchema:
    anchors: tuple[AnchorSpec, ...]
    traverse: tuple[TraverseSpec, ...] = ()
    as_of: str = AS_OF_TASK_CREATED
    budget_tokens: int | None = None

    @property
    def roots(self) -> frozenset[str]:
        return frozenset(spec.path.root for spec in self.anchors)


def _error(path: str, message: str, **details: Any) -> ValidationError:
    return ValidationError(INVALID, f"{path}: {message}", details={"path": path, **details})


def _unknown_keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _error(path, f"unknown keys {unknown}", unknown=unknown)


def _name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise _error(path, "must match [a-z][a-z0-9_]{0,62}")
    return value


def _int(value: Any, path: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _error(path, f"must be an integer {low}..{high}")
    return value


def parse_source(source: Any, *, where: str) -> Path:
    """An anchor's ``from`` in the (restricted) expression grammar of outcomes.

    ``description``/``title`` and ``$.<field>`` are the task itself; a path may
    also start at ``$.task`` or ``$.spawnedBy``. ``artifact[<type>].<field>``
    reads the artifact's metadata, as ``artifact[<type>].metadata.<field>`` does.
    """
    if not isinstance(source, str) or not source.strip():
        raise _error(where, "must be 'description', 'title' or a $. path")
    text = source.strip()
    if text in TEXT_SOURCES:
        text = f"$.task.{text}"
    if not text.startswith("$."):
        raise _error(where, "must be 'description', 'title' or a $. path", source=source)
    root = re.match(r"\$\.([A-Za-z]+)", text)
    if root is None or root.group(1) not in TASK_ROOTS:
        text = "$.task." + text[2:]
    text = _ARTIFACT_SHORT.sub(r"\1.metadata.", text)
    try:
        path = parse_path(text, where=where)
    except ValidationError as exc:
        raise _error(where, exc.message, source=source) from None
    if path.root not in TASK_ROOTS:
        raise _error(where, f"root must be one of {sorted(TASK_ROOTS)}", source=source)
    if path.required or path.truncate is not None:
        raise _error(where, "'!' and '|truncate' have no meaning for an anchor", source=source)
    return path


def _kinds(item: Mapping[str, Any], where: str) -> tuple[str, ...]:
    if "kind" in item and "kinds" in item:
        raise _error(where, "give either 'kind' or 'kinds'")
    if "kind" in item:
        return (_name(item["kind"], f"{where}.kind"),)
    raw = item.get("kinds", [])
    if not isinstance(raw, list) or len(raw) > MAX_KINDS:
        raise _error(f"{where}.kinds", f"must be a list of at most {MAX_KINDS} kinds")
    names = [_name(k, f"{where}.kinds[{i}]") for i, k in enumerate(raw)]
    return tuple(dict.fromkeys(names))


def _anchor(item: Any, where: str) -> AnchorSpec:
    if not isinstance(item, dict):
        raise _error(where, "must be an object")
    _unknown_keys(item, {"from", "kind", "kinds", "via"}, where)
    path = parse_source(item.get("from"), where=f"{where}.from")
    via = _name(item["via"], f"{where}.via") if item.get("via") is not None else None
    source = str(item["from"]).strip()
    return AnchorSpec(source=source, path=path, kinds=_kinds(item, where), via=via)


def _step(item: Any, where: str) -> TraverseSpec:
    if not isinstance(item, dict):
        raise _error(where, "must be an object")
    _unknown_keys(item, {"relation", "direction", "depth", "limit", "from"}, where)
    direction = item.get("direction", "out")
    if direction not in DIRECTIONS:
        raise _error(f"{where}.direction", f"must be one of {list(DIRECTIONS)}")
    start = item.get("from", "anchors")
    if start not in STEP_STARTS:
        raise _error(f"{where}.from", f"must be one of {list(STEP_STARTS)}")
    return TraverseSpec(
        relation=_name(item.get("relation"), f"{where}.relation"),
        direction=direction,
        depth=_int(item.get("depth", 1), f"{where}.depth", 1, MAX_DEPTH),
        limit=_int(item.get("limit", DEFAULT_STEP_LIMIT), f"{where}.limit", 1, MAX_STEP_LIMIT),
        start=start,
    )


def parse_context_schema(document: Any) -> ContextSchema | None:
    """Validate a ``context_schema`` document; ``{}`` means "no profile".

    A type version without a profile keeps the behaviour before CP-ADR-0064:
    its tasks get the free-text recall of ``POST /context`` and nothing else.
    """
    if document is None or document == {}:
        return None
    if not isinstance(document, dict):
        raise _error("contextSchema", "must be an object")
    guard_json_document(document, label="contextSchema", max_bytes=MAX_SCHEMA_BYTES)
    _unknown_keys(document, {"anchors", "traverse", "asOf", "budgetTokens"}, "contextSchema")
    raw_anchors = document.get("anchors")
    if not isinstance(raw_anchors, list) or not raw_anchors:
        raise _error("contextSchema.anchors", "must be a non-empty list")
    if len(raw_anchors) > MAX_ANCHOR_SPECS:
        raise _error("contextSchema.anchors", f"at most {MAX_ANCHOR_SPECS} anchors")
    raw_steps = document.get("traverse", [])
    if not isinstance(raw_steps, list) or len(raw_steps) > MAX_STEPS:
        raise _error("contextSchema.traverse", f"must be a list of at most {MAX_STEPS} steps")
    as_of = document.get("asOf", AS_OF_TASK_CREATED)
    if as_of not in AS_OF_MODES:
        raise _error("contextSchema.asOf", f"must be one of {list(AS_OF_MODES)}")
    budget = document.get("budgetTokens")
    if budget is not None:
        budget = _int(budget, "contextSchema.budgetTokens", 1, MAX_BUDGET_TOKENS)
    return ContextSchema(
        anchors=tuple(
            _anchor(item, f"contextSchema.anchors[{i}]") for i, item in enumerate(raw_anchors)
        ),
        traverse=tuple(
            _step(item, f"contextSchema.traverse[{i}]") for i, item in enumerate(raw_steps)
        ),
        as_of=as_of,
        budget_tokens=budget,
    )


# --- anchor candidates -------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One anchor value for Memory: ``{kind?, value}`` and where it came from."""

    value: str
    kind: str = ""
    source: str = ""
    via: str | None = None

    def to_request(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value} if self.kind else {"value": self.value}


def extract_identifiers(
    text: str,
    patterns: Mapping[str, Sequence[regex.Pattern[str]]],
    kinds: Sequence[str] = (),
    *,
    aliases: Mapping[str, str] | None = None,
    timeout: float = EXTRACTION_SECONDS,
) -> list[tuple[str, str]]:
    """``(kind, identifier)`` pairs found in ``text`` by the kinds' ``idPatterns``.

    Deterministic: kinds in the declared order (every kind of the catalog when
    none is declared), identifiers in the order they appear. A match lying
    inside a longer match is dropped — ``ADR-0062`` inside ``CP-ADR-0062``, a
    path inside ``POST <path>`` — so the more precise form is the one resolved.
    Containment is judged against the matches of every kind of the catalog,
    declared or not. The group ``id`` of a pattern, when present, is the
    identifier. A declared kind may be a synonym of a catalog kind
    (``kindAliases``); identifiers carry the canonical kind.

    Raises ``TimeoutError`` when the patterns take longer than ``timeout`` in
    all: a partial result would depend on the machine, so there is none.
    """
    text = (text or "")[:MAX_TEXT_CHARS]
    aliases = aliases or {}
    declared = [aliases.get(k, k) for k in kinds] if kinds else list(patterns.keys())
    wanted = [k for k in dict.fromkeys(declared) if k in patterns]
    # Every kind of the catalog is matched, the declared ones are returned: a
    # file name inside a path is not an event even when paths are not asked for.
    order = wanted + [k for k in patterns if k not in wanted]
    deadline = time.monotonic() + timeout
    found: list[tuple[int, int, int, str, str]] = []
    for rank, kind in enumerate(order):
        for pattern in patterns[kind]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("idPatterns took too long")
            for match in pattern.finditer(text, timeout=remaining):
                if len(found) >= MAX_MATCHES:
                    break
                group = "id" if "id" in pattern.groupindex else 0
                start = match.start(group)
                token = match.group(group) or ""
                stripped = token.strip().rstrip(_TRAILING)
                if not stripped or len(stripped) > MAX_CANDIDATE_CHARS:
                    continue
                start += token.index(stripped[0])
                found.append((rank, start, start + len(stripped), kind, stripped))
    kept = [f for f in _outermost(found) if f[0] < len(wanted)]
    kept.sort(key=lambda f: (f[0], f[1]))
    result: list[tuple[str, str]] = []
    for _, _, _, kind, token in kept:
        if (kind, token) not in result:
            result.append((kind, token))
    return result


def _outermost(
    found: list[tuple[int, int, int, str, str]],
) -> list[tuple[int, int, int, str, str]]:
    """Matches not lying inside a strictly longer match, in ``O(n log n)``.

    Sorted by start and then by descending end, every match seen before one
    starts no later; one of them contains it and is longer exactly when the
    furthest end seen so far (other spans) reaches its end.
    """
    ordered = sorted(found, key=lambda f: (f[1], -f[2]))
    kept: list[tuple[int, int, int, str, str]] = []
    furthest = -1
    i = 0
    while i < len(ordered):
        span = ordered[i][1:3]
        j = i
        while j < len(ordered) and ordered[j][1:3] == span:
            j += 1
        if furthest < span[1]:
            kept.extend(ordered[i:j])
        furthest = max(furthest, span[1])
        i = j
    return kept


def values_of(value: Any) -> list[str]:
    """Anchor values of a field: a string, or the strings of a list."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return [str(value)]
    return []


def anchor_candidates(
    schema: ContextSchema,
    sources: Mapping[str, Any],
    patterns: Mapping[str, Sequence[regex.Pattern[str]]],
    *,
    aliases: Mapping[str, str] | None = None,
    warnings: list[str] | None = None,
) -> list[Candidate]:
    """Anchor candidates of a task, at most :data:`MAX_ANCHORS`, in schema order.

    ``sources`` maps an anchor's ``source`` to its resolved value (the text of
    the description, a custom field, artifact metadata). A value is sent as
    written: Memory resolves it itself (normalized template parameters, alias,
    path suffix — ``matchedBy`` in its answer). Text whose extraction times out
    contributes nothing (and a warning); the values of fields still anchor.

    A credential-shaped value (the same guard as comments and evidence notes)
    is dropped with a warning: it is sent to Memory and recorded with the
    pack otherwise.
    """
    out: list[Candidate] = []
    seen: set[tuple[str, str, str | None]] = set()

    def add(values: Iterable[tuple[str, str]], spec: AnchorSpec) -> None:
        for kind, value in values:
            if secret_material(value) is not None:
                if warnings is not None:
                    warnings.append(f"a credential-shaped value of {spec.source} is not an anchor")
                continue
            value = value[:MAX_CANDIDATE_CHARS]
            key = (kind, value, spec.via)
            if key in seen or len(out) >= MAX_ANCHORS:
                continue
            seen.add(key)
            out.append(Candidate(value=value, kind=kind, source=spec.source, via=spec.via))

    for spec in schema.anchors:
        value = sources.get(spec.source)
        if spec.textual:
            text = value if isinstance(value, str) else ""
            try:
                found = extract_identifiers(text, patterns, spec.kinds, aliases=aliases)
            except TimeoutError:
                if warnings is not None:
                    warnings.append(f"identifier extraction from {spec.source} timed out")
                continue
            add(found, spec)
        else:
            kind = spec.kinds[0] if len(spec.kinds) == 1 else ""
            add(((kind, v) for v in values_of(value)), spec)
    return out
