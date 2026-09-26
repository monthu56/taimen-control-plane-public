"""Work graph vocabulary: where work comes from and how it is accepted (CP-ADR-0062).

Three documents travel with a work item and one with a Goal. They are stored
as JSON because their shape is small and closed, but they are validated here,
in one place, so that every writer — the HTTP API, an approval outcome, a
future rule engine — produces the same document:

* **origin** (a task) / **createdFrom** (a goal) — why the item exists:
  ``{kind, ref?, ruleId?, evidence[]}``. Immutable after creation: it is a
  record of how the item came to be, not a status.
* **acceptance** (a task) / **criteria** (a goal) — the checks that tell done
  from not done: ``[{key, kind, description, spec?}]``. A task's ``spec``
  follows a grammar per kind (CP-ADR-0067), checked here when the document
  is written; the verification stage executes it. A goal's ``spec`` stays
  opaque: nothing verifies goal criteria.
* **evidence** — pointers to facts: an observation, an artifact, an object
  in an external system or a task context pack (CP-ADR-0064), each
  optionally tied to one acceptance check.

Nothing here names a product, a business domain or a document format: a Goal is a
desired state of *something*, and what that something is lives in tenant data.
"""

import re
import uuid
from enum import StrEnum
from typing import Any

from control_plane.domain.approval_outcomes import MAX_INPUT_LENGTH, expressions_in
from control_plane.domain.artifact_schema import (
    CONTENT_OPTIONAL,
    CONTENT_REQUIRED,
    MAX_OUTPUT_MEDIA_TYPES,
    TYPE_KEY_RE,
    ArtifactSchema,
)
from control_plane.domain.artifact_type import is_media_pattern
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import guard_json_document, reject_secret_material
from control_plane.domain.redaction import reject_secret_text


class OriginKind(StrEnum):
    HUMAN = "human"  # a person filed it
    HARNESS = "harness"  # an agent or harness filed it during its own work
    RULE = "rule"  # a rule fired on observed facts (ruleId + evidence)
    PARENT = "parent"  # decomposition of another work item
    PROCESS = "process"  # a step of a declared process (e.g. an approval outcome)
    EXTERNAL = "external"  # imported from an external system


class CheckKind(StrEnum):
    DETERMINISTIC = "deterministic"  # a reproducible check (tests, a query)
    EXTERNAL_STATE = "external_state"  # a state observed in another system
    HUMAN = "human"  # a person judges
    LLM_JUDGE = "llm_judge"  # a model judges against a rubric


class EvidenceKind(StrEnum):
    OBSERVATION = "observation"
    ARTIFACT = "artifact"
    EXTERNAL = "external"
    # The knowledge context an executor was given (CP-ADR-0064).
    CONTEXT_PACK = "context_pack"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    ABANDONED = "abandoned"


ORIGIN_KINDS = frozenset(k.value for k in OriginKind)
CHECK_KINDS = frozenset(k.value for k in CheckKind)
EVIDENCE_KINDS = frozenset(k.value for k in EvidenceKind)
GOAL_STATUSES = frozenset(s.value for s in GoalStatus)

# Kinds whose origin is meaningless without saying WHICH parent, process step
# or external object: an unaddressed "it came from a process" is not a record.
_REF_REQUIRED = frozenset({OriginKind.PARENT, OriginKind.PROCESS, OriginKind.EXTERNAL})

MAX_REF_LENGTH = 512
MAX_RULE_ID_LENGTH = 200
MAX_NOTE_LENGTH = 1000
MAX_CHECK_DESCRIPTION_LENGTH = 2000
MAX_CHECKS = 50
MAX_ORIGIN_EVIDENCE = 50
MAX_EVIDENCE = 200
MAX_GOAL_TITLE_LENGTH = 500
MAX_DESIRED_STATE_LENGTH = 10_000
# Check spec documents are small parameter blocks, not payloads.
MAX_SPEC_BYTES = 16 * 1024

# A check spec that steps outside the grammar of its kind (CP-ADR-0067).
INVALID_ACCEPTANCE_SPEC = "invalid_acceptance_spec"
# Keys a spec of each kind may carry; anything else is refused.
SPEC_KEYS: dict[str, frozenset[str]] = {
    CheckKind.DETERMINISTIC: frozenset({"skill", "inputs", "expect", "artifact"}),
    CheckKind.EXTERNAL_STATE: frozenset({"event"}),
    CheckKind.HUMAN: frozenset({"approver", "approverRole"}),
    CheckKind.LLM_JUDGE: frozenset({"approver", "approverRole", "rubric"}),
}
# The advised order of checks: the cheap and reproducible first, people last.
# Advice only — the declared order is the order of execution.
CHECK_KIND_RANK: dict[str, int] = {
    CheckKind.DETERMINISTIC: 0,
    CheckKind.EXTERNAL_STATE: 1,
    CheckKind.HUMAN: 2,
    CheckKind.LLM_JUDGE: 2,
}
# What a ``deterministic`` check on an artifact of the task may say (CP-ADR-0072 §9).
ARTIFACT_SPEC_KEYS = frozenset({"type", "mediaTypes", "content"})
# Keys of the implicit checks of a task type's required outputs: reserved, an
# acceptance may not declare one (CP-ADR-0067, amendment 2026-09-26).
OUTPUT_CHECK_PREFIX = "output."
# Skill inputs of a check read the task being verified, nothing else.
SPEC_INPUT_ROOTS = frozenset({"task"})

CHECK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
# The key of the implicit ``external_state`` check a rule closing work without
# acceptance runs, and of the evidence it writes (CP-ADR-0063, amendment A1).
# Evidence may name it on any task: it is the rule's, never a declared check's.
RULE_EVIDENCE_CHECK = "rule-evidence"
RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")
# A pinned skill reference: the check must not change under a published task.
PINNED_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,126}@[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# An observation kind or a journal event type.
FACT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

HUMAN_ORIGIN: dict[str, Any] = {"kind": OriginKind.HUMAN.value, "evidence": []}


def _invalid(code: str, message: str, field: str, **details: Any) -> ValidationError:
    return ValidationError(code, message, details={"field": field, **details})


def _text(value: Any, *, field: str, max_length: int, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(code, f"{field} must be a non-empty string", field)
    text = value.strip()
    if len(text) > max_length:
        raise _invalid(
            "payload_too_large",
            f"{field} exceeds the {max_length}-character limit",
            field,
            maxLength=max_length,
        )
    reject_secret_text(text, code="secret_material_rejected", subject=field)
    return text


def _uuid(value: Any, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        raise _invalid("invalid_evidence", f"{field} must be a UUID", field) from None


# --- evidence -----------------------------------------------------------------


def normalize_evidence(
    items: Any, *, field: str = "evidence", limit: int = MAX_EVIDENCE
) -> list[dict[str, Any]]:
    """Validate a list of evidence pointers; returns the document to store.

    Each item names exactly one fact by the id it has where it lives: an
    observation id (the journal, CP-ADR-0057), an artifact id, or an external
    object ``{system, id, url?}``. Evidence is a pointer, not a copy — the
    optional ``note`` says why the fact matters, it does not restate it.
    """
    if not isinstance(items, list):
        raise _invalid("invalid_evidence", f"{field} must be a list", field)
    if len(items) > limit:
        raise _invalid("payload_too_large", f"{field} exceeds {limit} items", field, maxItems=limit)
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for index, item in enumerate(items):
        path = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise _invalid("invalid_evidence", f"{path} must be an object", path)
        kind = item.get("kind")
        allowed = {"kind", "check", "note"}
        normalized: dict[str, Any] = {"kind": kind}
        if kind == EvidenceKind.OBSERVATION:
            allowed.add("observationId")
            normalized["observationId"] = _uuid(
                item.get("observationId"), field=f"{path}.observationId"
            )
            identity: tuple[str, ...] = (kind, normalized["observationId"])
        elif kind == EvidenceKind.ARTIFACT:
            allowed.add("artifactId")
            normalized["artifactId"] = _uuid(item.get("artifactId"), field=f"{path}.artifactId")
            identity = (kind, normalized["artifactId"])
        elif kind == EvidenceKind.CONTEXT_PACK:
            allowed.add("contextPackId")
            normalized["contextPackId"] = _uuid(
                item.get("contextPackId"), field=f"{path}.contextPackId"
            )
            identity = (kind, normalized["contextPackId"])
        elif kind == EvidenceKind.EXTERNAL:
            allowed.add("externalRef")
            normalized["externalRef"] = _external_ref(
                item.get("externalRef"), field=f"{path}.externalRef"
            )
            identity = (
                kind,
                normalized["externalRef"]["system"],
                normalized["externalRef"]["id"],
            )
        else:
            raise _invalid(
                "invalid_evidence",
                f"{path}.kind must be one of {sorted(EVIDENCE_KINDS)}",
                f"{path}.kind",
                known=sorted(EVIDENCE_KINDS),
            )
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise _invalid(
                "invalid_evidence", f"{path} has unknown keys: {unknown}", path, unknown=unknown
            )
        if item.get("check") is not None:
            check = item["check"]
            if not isinstance(check, str) or not CHECK_KEY_RE.match(check):
                raise _invalid(
                    "invalid_evidence", f"{path}.check must be a check key", f"{path}.check"
                )
            normalized["check"] = check
            identity = (*identity, check)
        if item.get("note") is not None:
            normalized["note"] = _text(
                item["note"],
                field=f"{path}.note",
                max_length=MAX_NOTE_LENGTH,
                code="invalid_evidence",
            )
        # The same fact cited twice for the same check says nothing twice.
        if identity in seen:
            raise _invalid("duplicate_evidence", f"{path} repeats an earlier item", path)
        seen.add(identity)
        result.append(normalized)
    return result


def _external_ref(value: Any, *, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise _invalid("invalid_evidence", f"{field} must be an object", field)
    unknown = sorted(set(value) - {"system", "id", "url"})
    if unknown:
        raise _invalid(
            "invalid_evidence", f"{field} has unknown keys: {unknown}", field, unknown=unknown
        )
    ref = {
        "system": _text(
            value.get("system"), field=f"{field}.system", max_length=128, code="invalid_evidence"
        ),
        "id": _text(
            value.get("id"), field=f"{field}.id", max_length=MAX_REF_LENGTH, code="invalid_evidence"
        ),
    }
    if value.get("url") is not None:
        ref["url"] = _text(
            value["url"], field=f"{field}.url", max_length=2048, code="invalid_evidence"
        )
    return ref


def evidence_targets(*documents: list[dict[str, Any]]) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Observation and artifact ids named by normalized evidence lists.

    Existence is a question for the application layer (the journal and the
    artifact table); the domain only says which ids have to exist.
    """
    observations: set[uuid.UUID] = set()
    artifacts: set[uuid.UUID] = set()
    for items in documents:
        for item in items:
            if item["kind"] == EvidenceKind.OBSERVATION:
                observations.add(uuid.UUID(item["observationId"]))
            elif item["kind"] == EvidenceKind.ARTIFACT:
                artifacts.add(uuid.UUID(item["artifactId"]))
    return observations, artifacts


def context_pack_targets(*documents: list[dict[str, Any]]) -> set[uuid.UUID]:
    """Task context pack ids named by normalized evidence lists."""
    return {
        uuid.UUID(item["contextPackId"])
        for items in documents
        for item in items
        if item["kind"] == EvidenceKind.CONTEXT_PACK
    }


def check_evidence_against_acceptance(
    evidence: list[dict[str, Any]], acceptance: list[dict[str, Any]]
) -> None:
    """Evidence tied to a check must name a check the item actually declares.

    The implicit check of a rule closing the work (``RULE_EVIDENCE_CHECK``)
    is always known.
    """
    keys = {check["key"] for check in acceptance} | {RULE_EVIDENCE_CHECK}
    for index, item in enumerate(evidence):
        check = item.get("check")
        if check is not None and check not in keys:
            raise _invalid(
                "unknown_acceptance_check",
                f"evidence[{index}].check {check!r} is not declared in acceptance",
                f"evidence[{index}].check",
                check=check,
                known=sorted(keys),
            )


# --- origin -------------------------------------------------------------------


def normalize_origin(value: Any, *, field: str = "origin") -> dict[str, Any]:
    """Validate an origin document; returns the document to store.

    ``rule`` is the one kind with obligations of its own: a rule-born item has
    to say which rule fired (``ruleId``) and on which facts (``evidence``, at
    least one), otherwise nobody can tell a real divergence from a bad rule.
    ``ruleId`` on any other kind would be a claim nobody made, so it is
    refused rather than ignored.
    """
    if not isinstance(value, dict):
        raise _invalid("invalid_origin", f"{field} must be an object", field)
    unknown = sorted(set(value) - {"kind", "ref", "ruleId", "evidence"})
    if unknown:
        raise _invalid("invalid_origin", f"{field} has unknown keys: {unknown}", field)
    kind = value.get("kind")
    if kind not in ORIGIN_KINDS:
        raise _invalid(
            "invalid_origin",
            f"{field}.kind must be one of {sorted(ORIGIN_KINDS)}",
            f"{field}.kind",
            known=sorted(ORIGIN_KINDS),
        )
    origin: dict[str, Any] = {"kind": kind}
    if value.get("ref") is not None:
        origin["ref"] = _text(
            value["ref"], field=f"{field}.ref", max_length=MAX_REF_LENGTH, code="invalid_origin"
        )
    elif kind in _REF_REQUIRED:
        raise _invalid(
            "invalid_origin", f"{field}.ref is required for kind {kind!r}", f"{field}.ref"
        )

    rule_id = value.get("ruleId")
    if kind == OriginKind.RULE:
        if not isinstance(rule_id, str) or not RULE_ID_RE.match(rule_id):
            raise _invalid(
                "invalid_origin",
                f"{field}.ruleId is required for kind 'rule' and must match {RULE_ID_RE.pattern}",
                f"{field}.ruleId",
            )
        origin["ruleId"] = rule_id
    elif rule_id is not None:
        raise _invalid(
            "invalid_origin", f"{field}.ruleId is only allowed for kind 'rule'", f"{field}.ruleId"
        )

    evidence = normalize_evidence(
        value.get("evidence", []), field=f"{field}.evidence", limit=MAX_ORIGIN_EVIDENCE
    )
    if kind == OriginKind.RULE and not evidence:
        raise _invalid(
            "invalid_origin",
            f"{field}.evidence must name at least one fact for kind 'rule'",
            f"{field}.evidence",
        )
    for index, item in enumerate(evidence):
        # Origin evidence explains why the item exists; it cannot vouch for an
        # acceptance check of an item that did not exist yet.
        if "check" in item:
            raise _invalid(
                "invalid_origin",
                f"{field}.evidence[{index}] cannot reference an acceptance check",
                f"{field}.evidence[{index}].check",
            )
    origin["evidence"] = evidence
    return origin


# --- acceptance checks and goal criteria --------------------------------------


def normalize_checks(
    items: Any, *, field: str = "acceptance", typed_spec: bool = True
) -> list[dict[str, Any]]:
    """Validate a list of acceptance checks (or goal criteria).

    ``spec`` is bounded and scanned for secrets, since it is stored and read
    by agents. With ``typed_spec`` (a task's acceptance) it must also follow
    the grammar of its kind (``check_spec``), so that a check the
    verification stage cannot execute is refused when it is written, not
    when the work is done. Goal criteria pass ``typed_spec=False``: nothing
    executes them, and their ``spec`` stays opaque. A check without ``spec``
    is accepted as before.
    """
    if not isinstance(items, list):
        raise _invalid("invalid_acceptance", f"{field} must be a list", field)
    if len(items) > MAX_CHECKS:
        raise _invalid(
            "payload_too_large", f"{field} exceeds {MAX_CHECKS} checks", field, maxItems=MAX_CHECKS
        )
    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, item in enumerate(items):
        path = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise _invalid("invalid_acceptance", f"{path} must be an object", path)
        unknown = sorted(set(item) - {"key", "kind", "description", "spec"})
        if unknown:
            raise _invalid("invalid_acceptance", f"{path} has unknown keys: {unknown}", path)
        key = item.get("key")
        if not isinstance(key, str) or not CHECK_KEY_RE.match(key):
            raise _invalid(
                "invalid_acceptance",
                f"{path}.key must match {CHECK_KEY_RE.pattern}",
                f"{path}.key",
            )
        if typed_spec and key.startswith(OUTPUT_CHECK_PREFIX):
            raise _invalid(
                "invalid_acceptance",
                f"{path}.key: the prefix {OUTPUT_CHECK_PREFIX!r} is reserved for "
                "the required outputs of the task type",
                f"{path}.key",
            )
        if key in keys:
            raise _invalid("duplicate_check_key", f"{path}.key {key!r} is repeated", f"{path}.key")
        keys.add(key)
        kind = item.get("kind")
        if kind not in CHECK_KINDS:
            raise _invalid(
                "invalid_acceptance",
                f"{path}.kind must be one of {sorted(CHECK_KINDS)}",
                f"{path}.kind",
                known=sorted(CHECK_KINDS),
            )
        check: dict[str, Any] = {
            "key": key,
            "kind": kind,
            "description": _text(
                item.get("description"),
                field=f"{path}.description",
                max_length=MAX_CHECK_DESCRIPTION_LENGTH,
                code="invalid_acceptance",
            ),
        }
        if item.get("spec") is not None:
            spec = item["spec"]
            guard_json_document(spec, label=f"{path}.spec", max_bytes=MAX_SPEC_BYTES)
            reject_secret_material(spec, label=f"{path}.spec")
            if typed_spec:
                check_spec(kind, spec, field=f"{path}.spec")
            check["spec"] = spec
        result.append(check)
    return result


def _spec_error(field: str, kind: str, message: str) -> ValidationError:
    return _invalid(INVALID_ACCEPTANCE_SPEC, f"{field}: {message}", field, kind=kind)


def check_spec(kind: str, spec: dict[str, Any], *, field: str = "spec") -> None:
    """Check one ``spec`` against the grammar of its check kind (CP-ADR-0067).

    * ``deterministic`` — ``{skill: "name@version", inputs?, expect?}``: the
      same call as an approval outcome's ``invokeSkill`` (CP-ADR-0061) —
      inputs may read the task (``$.task.…``), ``expect`` holds the literal
      output values success requires; or ``{artifact: {type, mediaTypes?,
      content?}}``: an artifact of the task (CP-ADR-0072 §9) — never both;
    * ``external_state`` — ``{}`` or ``{event: "<observation kind or event
      type>"}``: passed by evidence tied to the check;
    * ``human`` — ``{}``, ``{approver: <principal id>}`` or
      ``{approverRole: <role id>}``: passed by a gate-approval decision;
    * ``llm_judge`` — as ``human``, plus an optional ``rubric`` for the
      person who decides: a model does not close the gate.

    Only the form is checked: whether the skill or the artifact type exists
    or which side effects the skill declares is for the application layer,
    which knows the registries.
    """
    unknown = sorted(set(spec) - SPEC_KEYS[kind])
    if unknown:
        raise _spec_error(field, kind, f"unknown keys for kind {kind!r}: {unknown}")
    if kind == CheckKind.DETERMINISTIC and "artifact" in spec:
        beside = sorted(set(spec) - {"artifact"})
        if beside:
            raise _spec_error(
                field, kind, f"artifact excludes skill, inputs and expect, got {beside}"
            )
        _check_spec_artifact(spec["artifact"], field=f"{field}.artifact", kind=kind)
    elif kind == CheckKind.DETERMINISTIC:
        skill = spec.get("skill")
        if not isinstance(skill, str) or not PINNED_SKILL_RE.match(skill):
            raise _spec_error(
                f"{field}.skill", kind, "skill must be a pinned reference name@version"
            )
        if "inputs" in spec:
            _check_spec_inputs(spec["inputs"], field=f"{field}.inputs", kind=kind)
        if "expect" in spec:
            _check_spec_expect(spec["expect"], field=f"{field}.expect", kind=kind)
    elif kind == CheckKind.EXTERNAL_STATE:
        if "event" in spec and (
            not isinstance(spec["event"], str) or not FACT_TYPE_RE.match(spec["event"])
        ):
            raise _spec_error(
                f"{field}.event", kind, "event must be an observation kind or an event type"
            )
    else:
        if "approver" in spec and "approverRole" in spec:
            raise _spec_error(field, kind, "approver and approverRole are mutually exclusive")
        for name in ("approver", "approverRole"):
            if name in spec and not _is_uuid(spec[name]):
                raise _spec_error(f"{field}.{name}", kind, f"{name} must be a UUID")
        if "rubric" in spec:
            rubric = spec["rubric"]
            if (
                not isinstance(rubric, str)
                or not rubric.strip()
                or len(rubric) > MAX_CHECK_DESCRIPTION_LENGTH
            ):
                raise _spec_error(
                    f"{field}.rubric",
                    kind,
                    f"rubric must be a non-empty string of at most "
                    f"{MAX_CHECK_DESCRIPTION_LENGTH} characters",
                )


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _check_spec_artifact(value: Any, *, field: str, kind: str) -> None:
    """``{type, mediaTypes?, content?}``: which artifact of the task passes."""
    if not isinstance(value, dict):
        raise _spec_error(field, kind, "artifact must be an object")
    unknown = sorted(set(value) - ARTIFACT_SPEC_KEYS)
    if unknown:
        raise _spec_error(field, kind, f"unknown keys for artifact: {unknown}")
    type_key = value.get("type")
    if not isinstance(type_key, str) or not TYPE_KEY_RE.match(type_key):
        raise _spec_error(f"{field}.type", kind, "type must be an artifact type key")
    if "mediaTypes" in value:
        media_types = value["mediaTypes"]
        if (
            not isinstance(media_types, list)
            or not media_types
            or len(media_types) > MAX_OUTPUT_MEDIA_TYPES
        ):
            raise _spec_error(
                f"{field}.mediaTypes",
                kind,
                f"mediaTypes must be a list of 1 to {MAX_OUTPUT_MEDIA_TYPES} media types",
            )
        for index, item in enumerate(media_types):
            if not isinstance(item, str) or not is_media_pattern(item.strip().lower()):
                raise _spec_error(
                    f"{field}.mediaTypes[{index}]",
                    kind,
                    "must be 'type/subtype', 'type/*' or '*/*'",
                )
    if "content" in value and value["content"] not in (CONTENT_REQUIRED, CONTENT_OPTIONAL):
        raise _spec_error(
            f"{field}.content",
            kind,
            f"content must be {CONTENT_REQUIRED!r} or {CONTENT_OPTIONAL!r}",
        )


def output_checks(schema: ArtifactSchema) -> list[dict[str, Any]]:
    """The implicit checks of a task type's required outputs, in declared order.

    Every completion runs them first, before the task's own acceptance
    (CP-ADR-0067, amendment 2026-09-26): a task whose type expects an output
    is not done without it, whatever its acceptance says.
    """
    checks: list[dict[str, Any]] = []
    for output in schema.outputs:
        if not output.required:
            continue
        artifact: dict[str, Any] = {"type": output.type}
        if output.media_types is not None:
            artifact["mediaTypes"] = list(output.media_types)
        artifact["content"] = output.content
        checks.append(
            {
                "key": f"{OUTPUT_CHECK_PREFIX}{output.key}",
                "kind": CheckKind.DETERMINISTIC.value,
                "description": f"Required output {output.key} ({output.type})",
                "spec": {"artifact": artifact},
            }
        )
    return checks


def _check_spec_inputs(value: Any, *, field: str, kind: str) -> None:
    """Skill inputs: JSON values; strings may read the task as ``$.task.…``."""
    if not isinstance(value, dict):
        raise _spec_error(field, kind, "inputs must be an object")
    for name, item in value.items():
        path = f"{field}.{name}"
        if isinstance(item, str):
            if len(item) > MAX_INPUT_LENGTH:
                raise _spec_error(path, kind, f"exceeds {MAX_INPUT_LENGTH} characters")
            try:
                paths = expressions_in(item, where=path)
            except ValidationError as exc:
                raise _spec_error(path, kind, exc.message) from None
            outside = sorted({p.text for p in paths if p.root not in SPEC_INPUT_ROOTS})
            if outside:
                raise _spec_error(path, kind, f"inputs may only read $.task, not {outside}")
        elif isinstance(item, dict):
            _check_spec_inputs(item, field=path, kind=kind)
        elif item is not None and not isinstance(item, bool | int | float):
            raise _spec_error(path, kind, "only strings, numbers, booleans, null and objects")


def _check_spec_expect(value: Any, *, field: str, kind: str) -> None:
    """``expect``: output fields and the literal values success requires."""
    if not isinstance(value, dict) or not value:
        raise _spec_error(field, kind, "expect must be a non-empty object")
    for name, item in value.items():
        path = f"{field}.{name}"
        if not OUTPUT_KEY_RE.match(name):
            raise _spec_error(path, kind, "not an output field name")
        if item is not None and not isinstance(item, str | bool | int | float):
            raise _spec_error(path, kind, "only strings, numbers, booleans, null")
        if isinstance(item, str) and "$." in item:
            raise _spec_error(path, kind, "expect takes literals, not expressions")


def check_order_advice(checks: list[dict[str, Any]]) -> list[str]:
    """Advice, not an error: checks declared out of the advised order.

    The stage runs checks in the declared order and stops at the first that
    fails; a person asked before a reproducible check has run may be asked
    for nothing. Returns one message per check that follows a check of a
    later-advised kind (deterministic → external_state → human/llm_judge).
    """
    advice: list[str] = []
    highest = -1
    for check in checks:
        rank = CHECK_KIND_RANK[check["kind"]]
        if rank < highest:
            advice.append(
                f"check {check['key']!r} ({check['kind']}) follows a check of a kind "
                "usually run later: deterministic -> external_state -> human/llm_judge"
            )
        highest = max(highest, rank)
    return advice


# --- goal ---------------------------------------------------------------------


def normalize_goal_title(value: Any) -> str:
    return _text(value, field="title", max_length=MAX_GOAL_TITLE_LENGTH, code="invalid_title")


def normalize_desired_state(value: Any) -> str:
    """The desired state is prose; empty is allowed (criteria may say it all)."""
    if not isinstance(value, str):
        raise _invalid("invalid_desired_state", "desiredState must be a string", "desiredState")
    text = value.strip()
    if len(text) > MAX_DESIRED_STATE_LENGTH:
        raise _invalid(
            "payload_too_large",
            f"desiredState exceeds the {MAX_DESIRED_STATE_LENGTH}-character limit",
            "desiredState",
            maxLength=MAX_DESIRED_STATE_LENGTH,
        )
    if text:
        reject_secret_text(text, code="secret_material_rejected", subject="desiredState")
    return text


def validate_goal_status(value: Any) -> str:
    if value not in GOAL_STATUSES:
        raise _invalid(
            "invalid_goal_status",
            f"status must be one of {sorted(GOAL_STATUSES)}",
            "status",
            known=sorted(GOAL_STATUSES),
        )
    return str(value)


def origin_summary(origin: dict[str, Any]) -> dict[str, Any]:
    """What the event journal may carry about an origin: references only.

    Notes are free text and stay with the item; ids, the rule and the kind are
    what a subscriber (and durable memory) needs to follow the chain.
    """
    summary: dict[str, Any] = {"kind": origin.get("kind")}
    for key in ("ref", "ruleId"):
        if origin.get(key) is not None:
            summary[key] = origin[key]
    evidence = []
    for item in origin.get("evidence", []):
        pointer = {
            k: item[k]
            for k in ("kind", "observationId", "artifactId", "contextPackId")
            if k in item
        }
        if "externalRef" in item:
            pointer["externalRef"] = {
                k: item["externalRef"][k] for k in ("system", "id") if k in item["externalRef"]
            }
        evidence.append(pointer)
    summary["evidence"] = evidence
    return summary
