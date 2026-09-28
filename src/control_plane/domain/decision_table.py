"""Decision tables of the process language (CP-ADR-0074 §2, FR-004).

A table (``$defs.decisionTable`` of the catalog schema) has inputs — CEL
expressions over the instance data, each with a type — outputs, rules and a
hit policy:

- ``first`` — the first rule that matches, in order;
- ``unique`` — exactly one rule may match; two rules matching one input are an
  error of the definition (an overlap);
- ``collect`` — every rule that matches, in order.

A condition cell is one of:

- ``-`` (or an empty cell, or no cell at all) — any value;
- a literal — ``go``, ``10``, ``true``, ``2026-01-01``;
- a list — ``a,b,c``;
- a range — ``[a..b)``, ``(a..b]``, an open end left empty: ``[10..)``; the
  comparisons ``<10``, ``<=10``, ``>10``, ``>=10`` are ranges too.

Ranges apply to ``number``, ``date`` and ``timestamp`` inputs; a ``string``
cell is a literal or a list, a ``boolean`` one ``true`` or ``false``.

The check finds overlaps (``unique``), rules that can never be reached
because earlier rules cover them (``first``) and gaps — inputs no rule
matches — with an example of such an input. It splits the domain of every
input into the cells the rules' conditions distinguish and searches them
depth first, so its work follows the rules, not the size of the domain;
a table too large to search fully says so instead of guessing.

Pure functions over plain values; no I/O.
"""

import difflib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

INPUT_TYPES = ("string", "number", "boolean", "date", "timestamp")
OUTPUT_TYPES = ("string", "number", "boolean", "date", "duration", "object", "array")
HIT_POLICIES = ("first", "unique", "collect")

# How many cells the gap and reachability search may visit per question.
SEARCH_BUDGET = 50_000


class DecisionError(ValueError):
    """A cell that does not parse, or an evaluation without a single answer."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


# --- conditions ---------------------------------------------------------------


@dataclass(frozen=True)
class AnyValue:
    """``-``: matches every value."""

    def matches(self, value: Any) -> bool:
        return True

    def label(self) -> str:
        return "-"


@dataclass(frozen=True)
class OneOf:
    """A literal or a list of literals."""

    values: frozenset[Any]

    def matches(self, value: Any) -> bool:
        return value in self.values

    def label(self) -> str:
        return ",".join(sorted(_show(v) for v in self.values))


@dataclass(frozen=True)
class Range:
    """An interval over numbers (dates as day ordinals, timestamps as seconds).

    ``None`` bounds are open to infinity. Date ranges are stored closed: over
    whole days ``(1..5)`` is ``[2..4]``, which keeps intersection exact.
    """

    lo: float | None
    lo_closed: bool
    hi: float | None
    hi_closed: bool

    def matches(self, value: Any) -> bool:
        if not isinstance(value, int | float) or isinstance(value, bool):
            return False
        if self.lo is not None and (value < self.lo or (value == self.lo and not self.lo_closed)):
            return False
        return not (
            self.hi is not None and (value > self.hi or (value == self.hi and not self.hi_closed))
        )

    def empty(self) -> bool:
        if self.lo is None or self.hi is None:
            return False
        return self.lo > self.hi or (self.lo == self.hi and not (self.lo_closed and self.hi_closed))

    def label(self) -> str:
        left = "[" if self.lo_closed else "("
        right = "]" if self.hi_closed else ")"
        lo = "" if self.lo is None else _show(self.lo)
        hi = "" if self.hi is None else _show(self.hi)
        return f"{left}{lo}..{hi}{right}"


Condition = AnyValue | OneOf | Range
ANY = AnyValue()


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# --- values of an input type -----------------------------------------------------


def _number(text: Any) -> float:
    if isinstance(text, bool):
        raise ValueError("not a number")
    number = float(text) if isinstance(text, int | float) else float(str(text).strip())
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return number


def _date(text: Any) -> float:
    if isinstance(text, datetime):
        return float(text.date().toordinal())
    if isinstance(text, date):
        return float(text.toordinal())
    return float(date.fromisoformat(str(text).strip()).toordinal())


def _timestamp(text: Any) -> float:
    moment = text if isinstance(text, datetime) else datetime.fromisoformat(str(text).strip())
    if moment.tzinfo is None:
        raise ValueError("a timestamp needs its offset (RFC 3339)")
    return moment.astimezone(UTC).timestamp()


def _boolean(text: Any) -> bool:
    if isinstance(text, bool):
        return text
    word = str(text).strip().lower()
    if word in ("true", "false"):
        return word == "true"
    raise ValueError("not true or false")


def _string(text: Any) -> str:
    if isinstance(text, bool):
        return "true" if text else "false"
    word = str(text).strip()
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        word = word[1:-1]
    return word


_ORDERED = {"number": _number, "date": _date, "timestamp": _timestamp}


def value_of(kind: str, raw: Any) -> Any:
    """A runtime value as the conditions of a ``kind`` input compare it."""
    if kind in _ORDERED:
        return _ORDERED[kind](raw)
    if kind == "boolean":
        return _boolean(raw)
    return raw if isinstance(raw, str) else _string(raw)


_RANGE = re.compile(r"^\s*([\[\(\]])\s*(.*?)\s*\.\.\s*(.*?)\s*([\]\)\[])\s*$")
_COMPARISON = re.compile(r"^\s*(<=|>=|<|>)\s*(.+?)\s*$")


def parse_condition(cell: Any, kind: str) -> Condition:
    """The condition a cell states for an input of type ``kind``."""
    if cell is None or (isinstance(cell, str) and cell.strip() in ("", "-")):
        return ANY
    try:
        return _parse(cell, kind)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DecisionError(
            "invalid_table_cell",
            f"{_show(cell)!r} is not a condition of a {kind} input: {exc}",
            hint=_CELL_HINTS[kind],
        ) from None


_CELL_HINTS = {
    "string": "a literal or a list a,b",
    "boolean": "true, false or -",
    "number": "a literal, a list 1,2, a range [0..10) or a comparison >=10",
    "date": "a date 2026-01-01, a list or a range [2026-01-01..2026-02-01)",
    "timestamp": "an RFC 3339 time, a list or a range",
}


def _parse(cell: Any, kind: str) -> Condition:
    if kind == "boolean":
        return OneOf(frozenset({_boolean(cell)}))
    if kind not in _ORDERED:
        if not isinstance(cell, str):
            return OneOf(frozenset({_string(cell)}))
        return OneOf(frozenset(_string(part) for part in cell.split(",")))
    convert = _ORDERED[kind]
    if not isinstance(cell, str):
        return OneOf(frozenset({convert(cell)}))
    ranged = _RANGE.match(cell)
    if ranged is not None:
        left, lo_text, hi_text, right = ranged.groups()
        lo = convert(lo_text) if lo_text else None
        hi = convert(hi_text) if hi_text else None
        return _range(kind, lo, left == "[" and lo is not None, hi, right == "]" and hi is not None)
    compared = _COMPARISON.match(cell)
    if compared is not None:
        operator, bound_text = compared.groups()
        bound = convert(bound_text)
        if operator.startswith("<"):
            return _range(kind, None, False, bound, operator == "<=")
        return _range(kind, bound, operator == ">=", None, False)
    return OneOf(frozenset(convert(part) for part in cell.split(",")))


def _range(
    kind: str, lo: float | None, lo_closed: bool, hi: float | None, hi_closed: bool
) -> Range:
    if kind == "date":
        if lo is not None and not lo_closed:
            lo, lo_closed = lo + 1, True
        if hi is not None and not hi_closed:
            hi, hi_closed = hi - 1, True
    found = Range(lo, lo_closed, hi, hi_closed)
    if found.empty():
        raise ValueError("the range is empty")
    return found


# --- the table ------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    index: int
    conditions: tuple[Condition, ...]
    then: Mapping[str, Any]


@dataclass(frozen=True)
class Table:
    """A parsed table: input ids and types, output ids, rules."""

    id: str
    hit_policy: str
    inputs: tuple[str, ...]
    input_types: tuple[str, ...]
    outputs: tuple[str, ...]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Finding:
    """What the check found in one table; ``rule`` is its index, ``input`` an input id."""

    code: str
    severity: str
    message: str
    rule: int | None = None
    input: str | None = None
    output: str | None = None
    hint: str | None = None


_OUTPUT_CHECKS: Mapping[str, Any] = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "date": lambda v: isinstance(v, str),
    "duration": lambda v: isinstance(v, str),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def build(spec: Mapping[str, Any], input_types: Sequence[str]) -> tuple[Table, list[Finding]]:
    """Parse a table whose shape the catalog schema has checked.

    ``input_types`` are the resolved types of the inputs (declared, or derived
    from their expressions by the caller). A cell that does not parse, a
    condition on an unknown input or an output the table does not declare is a
    finding; such a cell reads as ``-`` so that the rest can still be checked.
    """
    findings: list[Finding] = []
    inputs = tuple(str(item["id"]) for item in spec["inputs"])
    outputs = tuple(str(item["id"]) for item in spec["outputs"])
    output_types = {str(item["id"]): item.get("type") for item in spec["outputs"]}
    for kind, ids in (("input", inputs), ("output", outputs)):
        for name in sorted({i for i in ids if ids.count(i) > 1}):
            findings.append(
                Finding(f"duplicate_table_{kind}", "error", f"{kind} {name!r} is declared twice")
            )
    rules: list[Rule] = []
    for index, raw in enumerate(spec["rules"]):
        when = raw.get("when") or {}
        for name in when:
            if name not in inputs:
                findings.append(
                    Finding(
                        "unknown_table_input",
                        "error",
                        f"the table has no input {name!r}",
                        rule=index,
                        input=name,
                        hint=_close(name, inputs),
                    )
                )
        conditions: list[Condition] = []
        for name, kind in zip(inputs, input_types, strict=True):
            try:
                conditions.append(parse_condition(when.get(name), kind))
            except DecisionError as exc:
                findings.append(
                    Finding(exc.code, "error", exc.message, rule=index, input=name, hint=exc.hint)
                )
                conditions.append(ANY)
        then = raw.get("then") or {}
        for name, value in then.items():
            if name not in outputs:
                findings.append(
                    Finding(
                        "unknown_table_output",
                        "error",
                        f"the table has no output {name!r}",
                        rule=index,
                        output=name,
                        hint=_close(name, outputs),
                    )
                )
                continue
            output_type = output_types.get(name)
            if (
                output_type is not None
                and value is not None
                and not _OUTPUT_CHECKS[output_type](value)
            ):
                findings.append(
                    Finding(
                        "table_output_type_mismatch",
                        "error",
                        f"output {name!r} is {output_type}, the rule gives {_show(value)!r}",
                        rule=index,
                        output=name,
                    )
                )
        for name in outputs:
            if name not in then:
                findings.append(
                    Finding(
                        "table_output_missing",
                        "error",
                        f"the rule does not give output {name!r}",
                        rule=index,
                        output=name,
                    )
                )
        rules.append(Rule(index, tuple(conditions), dict(then)))
    table = Table(
        id=str(spec["id"]),
        hit_policy=str(spec["hitPolicy"]),
        inputs=inputs,
        input_types=tuple(input_types),
        outputs=outputs,
        rules=tuple(rules),
    )
    return table, findings


def _close(name: str, known: Sequence[str]) -> str | None:
    close = difflib.get_close_matches(name, list(known), n=1)
    if close:
        return f"did you mean {close[0]}?"
    return f"declared: {', '.join(known)}" if known else None


# --- intersection, containment ---------------------------------------------------------


def intersects(a: Condition, b: Condition) -> bool:
    if isinstance(a, AnyValue) or isinstance(b, AnyValue):
        return True
    if isinstance(a, OneOf) and isinstance(b, OneOf):
        return bool(a.values & b.values)
    if isinstance(a, OneOf):
        return any(b.matches(v) for v in a.values)
    if isinstance(b, OneOf):
        return any(a.matches(v) for v in b.values)
    assert isinstance(a, Range) and isinstance(b, Range)
    lo, lo_closed = _max_lo(a, b)
    hi, hi_closed = _min_hi(a, b)
    return not Range(lo, lo_closed, hi, hi_closed).empty()


def _max_lo(a: Range, b: Range) -> tuple[float | None, bool]:
    if a.lo is None:
        return b.lo, b.lo_closed
    if b.lo is None or a.lo > b.lo:
        return a.lo, a.lo_closed
    if b.lo > a.lo:
        return b.lo, b.lo_closed
    return a.lo, a.lo_closed and b.lo_closed


def _min_hi(a: Range, b: Range) -> tuple[float | None, bool]:
    if a.hi is None:
        return b.hi, b.hi_closed
    if b.hi is None or a.hi < b.hi:
        return a.hi, a.hi_closed
    if b.hi < a.hi:
        return b.hi, b.hi_closed
    return a.hi, a.hi_closed and b.hi_closed


# --- cells: the pieces of an input's domain the rules tell apart -----------------------


@dataclass(frozen=True)
class _Cell:
    value: Any  # a representative value
    label: str


_OTHER = object()  # a string or value no rule names


def _cells(kind: str, conditions: Sequence[Condition], within: Condition) -> list[_Cell]:
    """Representatives of every piece of the domain ``conditions`` distinguish, in ``within``."""
    if kind == "boolean":
        found = [_Cell(True, "true"), _Cell(False, "false")]
    elif kind in _ORDERED:
        found = _ordered_cells(kind, [*conditions, within])
    else:
        named = sorted(
            {v for c in (*conditions, within) if isinstance(c, OneOf) for v in c.values}, key=str
        )
        found = [_Cell(v, repr(v)) for v in named]
        found.append(_Cell(_OTHER, "any other value"))
    return [cell for cell in found if within.matches(cell.value)]


def _ordered_cells(kind: str, conditions: Sequence[Condition]) -> list[_Cell]:
    points: set[float] = set()
    for condition in conditions:
        if isinstance(condition, OneOf):
            points.update(condition.values)
        elif isinstance(condition, Range):
            points.update(p for p in (condition.lo, condition.hi) if p is not None)
    ordered = sorted(points)
    discrete = kind == "date"
    render = _render(kind)
    if not ordered:
        return [_Cell(0.0, "any value")]
    cells = [_Cell(ordered[0] - 1, f"< {render(ordered[0])}")]
    for index, point in enumerate(ordered):
        cells.append(_Cell(point, render(point)))
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        if following is None:
            cells.append(_Cell(point + 1, f"> {render(point)}"))
        elif not discrete:
            cells.append(_Cell((point + following) / 2, f"({render(point)}..{render(following)})"))
        elif following - point > 1:
            cells.append(_Cell(point + 1, f"({render(point)}..{render(following)})"))
    return cells


def _render(kind: str) -> Any:
    if kind == "date":
        return lambda v: date.fromordinal(int(v)).isoformat()
    if kind == "timestamp":
        return lambda v: datetime.fromtimestamp(v, UTC).isoformat().replace("+00:00", "Z")
    return _show


class _Budget:
    def __init__(self, limit: int) -> None:
        self.left = limit

    def spend(self) -> bool:
        self.left -= 1
        return self.left >= 0


class SearchTruncated(Exception):
    """The table is too large to search within :data:`SEARCH_BUDGET`."""


def find_gap(
    table: Table, rules: Sequence[Rule], within: Sequence[Condition] | None = None
) -> dict[str, str] | None:
    """An input (by cell label) inside ``within`` that none of ``rules`` matches, or ``None``.

    Raises :class:`SearchTruncated` when the search runs out of budget.
    """
    region = tuple(within) if within is not None else tuple(ANY for _ in table.inputs)
    budget = _Budget(SEARCH_BUDGET)
    found = _gap(table, list(rules), region, 0, {}, budget)
    return found


def _gap(
    table: Table,
    rules: list[Rule],
    region: tuple[Condition, ...],
    position: int,
    chosen: dict[str, str],
    budget: _Budget,
) -> dict[str, str] | None:
    if not budget.spend():
        raise SearchTruncated
    if not rules:
        example = dict(chosen)
        for name, condition in zip(table.inputs[position:], region[position:], strict=True):
            example[name] = "any value" if isinstance(condition, AnyValue) else condition.label()
        return example
    if position == len(table.inputs) or any(
        all(isinstance(c, AnyValue) for c in rule.conditions[position:]) for rule in rules
    ):
        return None
    name, kind = table.inputs[position], table.input_types[position]
    conditions = [rule.conditions[position] for rule in rules]
    for cell in _cells(kind, conditions, region[position]):
        matching = [rule for rule in rules if rule.conditions[position].matches(cell.value)]
        found = _gap(table, matching, region, position + 1, {**chosen, name: cell.label}, budget)
        if found is not None:
            return found
    return None


def check(table: Table) -> list[Finding]:
    """Overlaps, unreachable rules and gaps of a parsed table."""
    findings: list[Finding] = []
    rules = table.rules
    try:
        if table.hit_policy == "unique":
            for later in rules:
                for earlier in rules[: later.index]:
                    if all(
                        intersects(a, b)
                        for a, b in zip(earlier.conditions, later.conditions, strict=True)
                    ):
                        findings.append(
                            Finding(
                                "table_overlap",
                                "error",
                                f"rules {earlier.index} and {later.index} both match some input"
                                " of a unique table",
                                rule=later.index,
                                hint="narrow one of them, or use hitPolicy first",
                            )
                        )
                        break
        if table.hit_policy == "first":
            for later in rules[1:]:
                covering = [r for r in rules[: later.index] if _overlaps(r, later)]
                if covering and find_gap(table, covering, later.conditions) is None:
                    findings.append(
                        Finding(
                            "table_rule_unreachable",
                            "warning",
                            f"rule {later.index} never applies: earlier rules match every"
                            " input it matches",
                            rule=later.index,
                        )
                    )
        if table.hit_policy != "collect":
            gap = find_gap(table, rules)
            if gap is not None:
                shown = ", ".join(f"{name} = {label}" for name, label in gap.items())
                findings.append(
                    Finding(
                        "table_gap",
                        "warning",
                        f"no rule matches the input {shown}",
                        hint="add a rule for it or a last rule with '-' in every input",
                    )
                )
    except SearchTruncated:
        findings.append(
            Finding(
                "table_check_truncated",
                "warning",
                "the table is too large to check it for gaps and unreachable rules completely",
            )
        )
    return findings


def _overlaps(a: Rule, b: Rule) -> bool:
    return all(intersects(x, y) for x, y in zip(a.conditions, b.conditions, strict=True))


# --- evaluation ------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The answer of a table: the outputs (a list of them for ``collect``) and the rules used."""

    result: Any
    rules: tuple[int, ...]


def evaluate(table: Table, values: Mapping[str, Any]) -> Decision:
    """Apply the table to input values (the values of the input expressions, by input id).

    ``first`` without a match and ``unique`` without exactly one match raise
    :class:`DecisionError` (``decision_no_match``, ``decision_ambiguous``); a
    value that is not of its input's type (or ``null``) matches only ``-``.
    """
    typed: list[Any] = []
    for name, kind in zip(table.inputs, table.input_types, strict=True):
        raw = values.get(name)
        try:
            typed.append(_OTHER if raw is None else value_of(kind, raw))
        except (ValueError, TypeError, OverflowError):
            typed.append(_OTHER)
    matched = [
        rule
        for rule in table.rules
        if all(c.matches(v) for c, v in zip(rule.conditions, typed, strict=True))
    ]
    if table.hit_policy == "collect":
        return Decision([dict(rule.then) for rule in matched], tuple(r.index for r in matched))
    if not matched:
        raise DecisionError("decision_no_match", f"no rule of table {table.id!r} matches")
    if table.hit_policy == "unique" and len(matched) > 1:
        raise DecisionError(
            "decision_ambiguous",
            f"rules {[r.index for r in matched]} of unique table {table.id!r} all match",
        )
    return Decision(dict(matched[0].then), (matched[0].index,))
