"""Work executed by a Skill: ``task_types.execution`` (ADR-0056 §3).

A task type version may declare ``execution = {skill, version, inputs}``. A
task of that type is then executed by the skill executor of the agent daemon:
claim, run, and inside the run exactly one ``skill_invocation`` whose inputs
are taken from the task. ``inputs`` says how, with simple JSON paths over the
task as the API returns it (camelCase):

- a string — one path whose value is the whole input object, by default
  ``$.customFields``;
- an object ``{inputName: path}`` — each input taken from its own path.

The grammar is deliberately tiny — ``$`` followed by ``.name`` and ``[index]``
segments — because the daemon applies it without any domain knowledge
(TAI-ADR-0041): it is transport by contract, not a query language. The daemon
carries its own copy of the evaluator; this module only validates.

Pure functions only: no I/O, no database.
"""

import re
from typing import Any

from control_plane.domain.errors import ValidationError

DEFAULT_INPUTS_PATH = "$.customFields"
EXECUTION_FIELDS = frozenset({"skill", "version", "inputs"})
MAX_INPUT_MAPPINGS = 100
MAX_PATH_LENGTH = 500

_SEGMENT = r"(\.[A-Za-z_][A-Za-z0-9_\-]*|\[\d{1,6}\])"
PATH_RE = re.compile(rf"^\$(?:{_SEGMENT})*$")
_INPUT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,99}$")


def _invalid(message: str, **details: Any) -> ValidationError:
    return ValidationError("invalid_task_execution", message, details=details)


def _path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_PATH_LENGTH or not PATH_RE.match(value):
        raise _invalid(
            "an input path must look like $.customFields.name or $.items[0]",
            field=field,
        )
    return value


def normalize_execution(value: Any) -> dict[str, Any] | None:
    """Validate ``execution`` and return it with ``inputs`` filled in.

    Whether the named skill version exists and is invocable is a question for
    the database; the caller answers it.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("execution must be an object {skill, version, inputs}", field="execution")
    unknown = sorted(set(value) - EXECUTION_FIELDS)
    if unknown:
        raise _invalid("Unknown execution fields", field="execution", unknown=unknown)
    skill = value.get("skill")
    version = value.get("version")
    if not isinstance(skill, str) or not skill or len(skill) > 200 or "@" in skill:
        raise _invalid("execution.skill must be a skill name", field="execution.skill")
    if not isinstance(version, str) or not version or len(version) > 50:
        # A pinned version, never a bare name: the type version is immutable,
        # and "newest active" would change what it means under a live task.
        raise _invalid("execution.version must pin a skill version", field="execution.version")

    inputs = value.get("inputs", DEFAULT_INPUTS_PATH)
    if isinstance(inputs, str):
        normalized: str | dict[str, str] = _path(inputs, field="execution.inputs")
    elif isinstance(inputs, dict):
        if len(inputs) > MAX_INPUT_MAPPINGS:
            raise _invalid("too many input mappings", field="execution.inputs")
        normalized = {}
        for name, path in inputs.items():
            if not _INPUT_NAME_RE.match(name):
                raise _invalid("invalid input name", field="execution.inputs", name=name[:100])
            normalized[name] = _path(path, field=f"execution.inputs.{name}")
    else:
        raise _invalid(
            "execution.inputs must be a path or an object {inputName: path}",
            field="execution.inputs",
        )
    return {"skill": skill, "version": version, "inputs": normalized}


__all__ = ["DEFAULT_INPUTS_PATH", "PATH_RE", "normalize_execution"]
