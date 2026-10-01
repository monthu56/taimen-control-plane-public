"""Machine-readable repository conventions: ``.agents/runner.yaml`` (universal-runner U003).

A repository of the catalog carries, beside its prose conventions
(``AGENTS.md``), a small file the daemon acts on: which neighbours to check out
beside the working copy, how to set it up, which test services a run needs and
which checks to run before hand-in. This module is the format: the JSON Schema
beside it (``runner_config.schema.json``), the page that documents it
(``runner_config.md``) and the parser the daemon uses.

The file is read from the base revision of the task only (TAI-ADR-0063 §4), so
a task branch cannot change what the daemon does for it. Parsing is strict: an
unknown field, a check naming an undeclared service or an ``env`` value with an
unknown placeholder is an error, not something ignored, and every error names
the field it is about (``$.services.db.template``), so the author of the file
can fix it without guessing.

``env`` values of a service may use the placeholders ``{user}``,
``{password}``, ``{host}``, ``{port}`` and ``{database}``; the daemon fills them
from what the node handed out for the run (:meth:`ServiceSpec.render_env`).
``{{`` and ``}}`` stand for literal braces.

``setup`` and ``checks[].run`` are shell commands (``sh -c``, :data:`SHELL`)
run in the root of the working copy; nothing is substituted in them.

Run as a script, the module checks a file and prints its errors without a
traceback: ``python -m control_plane_agent.runner_config .agents/runner.yaml``,
or, outside the environment of control-plane, ``python
<control-plane>/src/control_plane_agent/runner_config.py`` with ``pyyaml`` and
``jsonschema`` installed (the module imports nothing else).
"""

from __future__ import annotations

import json
import logging
import re
import string
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from jsonschema import Draft202012Validator

logger = logging.getLogger("control_plane_agent.runner_config")

#: Where the file lives in a repository, relative to its root.
RUNNER_CONFIG_PATH = ".agents/runner.yaml"
SCHEMA_FILE = Path(__file__).with_name("runner_config.schema.json")
SUPPORTED_VERSION = 1
#: What a service ``env`` value may refer to, filled from the handed-out service.
PLACEHOLDERS = ("user", "password", "host", "port", "database")
#: Names a service ``env`` cannot set: the daemon, the node and the executor own
#: them, or they change what the executor runs (the loader, the interpreter, the
#: shell), where its requests go and whom it trusts (proxies, CA bundles,
#: ``ANTHROPIC_BASE_URL``) or whose credentials it uses (``IAM_*``, ``SSH_*``,
#: ``GH_*``, and ``XDG_*``: the client looks its credentials file up by
#: ``XDG_CONFIG_HOME``). ``*`` stands for any rest of the
#: name; the entries of :data:`ANY_CASE_ENV_NAMES` are matched in any case (the
#: tools read them so), everything else exactly.
#: The schema's ``$defs.reservedEnvName`` says the same; a test holds them equal.
RESERVED_ENV_NAMES = (
    "PATH",
    "HOME",
    "SHELL",
    "ENV",
    "BASH_ENV",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "NODE_OPTIONS",
    "NODE_PATH",
    "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "PERL5OPT",
    "RUBYOPT",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "CURL_HOME",
    "LD_*",
    "DYLD_*",
    "XDG_*",
    "GIT_*",
    "GH_*",
    "SSH_*",
    "UV_*",
    "PIP_*",
    "NPM_CONFIG_*",
    "FLEET_*",
    "CLAUDE_*",
    "ANTHROPIC_*",
    "OPENAI_*",
    "CODEX_*",
    "OPENCODE_*",
    "IAM_*",
    "CONTROL_PLANE_*",
    "*_PROXY",
)
#: Reserved entries matched in any case: ``http_proxy``, ``npm_config_registry``.
ANY_CASE_ENV_NAMES = ("NPM_CONFIG_*", "*_PROXY")
#: How ``setup`` and ``checks[].run`` are run: ``[*SHELL, command]``, in the working copy root.
SHELL = ("sh", "-c")
ROOT = "$"
_UNSUPPORTED_VERSION = f"unsupported version, expected {SUPPORTED_VERSION}"

_PLAIN_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
#: The rule of each name pattern of ``$defs``, said in words: a raw regular
#: expression in an error message tells the author of the file nothing.
NAME_RULES = {
    "repositoryKey": (
        "a repository key: lowercase latin letters, digits and '-', "
        "not starting with '-', 1-63 characters"
    ),
    "serviceName": (
        "a service name: lowercase latin letters, digits and '-', "
        "not starting or ending with '-', 1-31 characters"
    ),
    "templateName": (
        "a template name: lowercase latin letters, digits and '-', "
        "not starting with '-', 1-63 characters"
    ),
    "checkName": (
        "a check name: lowercase latin letters, digits, '.', '_' and '-', "
        "starting with a letter or a digit, 1-63 characters"
    ),
    "envName": (
        "a variable name: latin letters, digits and '_', "
        "not starting with a digit, 1-128 characters"
    ),
}


@dataclass(frozen=True)
class FieldError:
    """One problem of the file, tied to the field it is about."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a key repeated in one mapping, and merge keys.

    Plain YAML loading keeps the last of two ``db:`` keys and silently drops the
    first; here that is an error at the line of the repeat. A merge key
    (``<<: *base``) would bring keys in past that check, and one written next to
    its own ``db:`` would be overridden without a word, so it is refused too.
    """

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        for key_node, _value in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "merge keys (<<) are not supported; write the keys out",
                    key_node.start_mark,
                )
        super().flatten_mapping(node)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _value in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                repeated = key in seen
            except TypeError:
                continue  # an unhashable key; the base class reports it
            if repeated:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class RunnerConfigError(ValueError):
    """The file is not a valid ``runner.yaml``; ``errors`` says where and why."""

    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__(f"{RUNNER_CONFIG_PATH} is invalid: " + "; ".join(map(str, errors)))


@dataclass(frozen=True)
class ServiceCredentials:
    """What the node hands out for one service of a run."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str


@dataclass(frozen=True)
class ServiceSpec:
    """One entry of ``services``; checked on construction, not only by the parser."""

    template: str
    env: Mapping[str, str]

    def __post_init__(self) -> None:
        problems = []
        if not isinstance(self.template, str) or not _matches("templateName", self.template):
            problems.append(f"template {self.template!r} is not {NAME_RULES['templateName']}")
        if not isinstance(self.env, Mapping):
            raise ValueError(f"invalid service: env is not a mapping: {type(self.env).__name__}")
        # A copy behind a read-only view: neither the caller's dict nor a
        # later holder of the spec can change what was checked here.
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        for name, value in self.env.items():
            if not isinstance(name, str) or not _matches("envName", name):
                problems.append(f"env name {name!r} is not {NAME_RULES['envName']}")
            elif is_reserved_env_name(name):
                problems.append(f"env name {name!r} is reserved ({reserved_entry(name)})")
            if not isinstance(value, str):
                problems.append(f"env {name!r} is not a string")
            else:
                problems += [f"env {name!r}: {p}" for p in placeholder_errors(value)]
        if problems:
            raise ValueError("invalid service: " + "; ".join(problems))

    def render_env(self, credentials: ServiceCredentials) -> dict[str, str]:
        """``env`` with the placeholders filled from ``credentials``.

        The values were checked by :func:`parse_runner_config`, so formatting
        cannot reach anything but the five placeholders.
        """
        values = {
            "user": credentials.user,
            "password": credentials.password,
            "host": credentials.host,
            "port": str(credentials.port),
            "database": credentials.database,
        }
        return {name: template.format_map(values) for name, template in self.env.items()}

    def render_secrets(self, credentials: ServiceCredentials) -> tuple[str, ...]:
        """What of :meth:`render_env` a run's output must not show.

        The password itself, and every ``env`` value that has ``{password}``
        or ``{user}`` filled in: a connection URL carries both. Values with
        only ``{host}``, ``{port}`` or ``{database}`` are no secret, and
        masking them would garble every path and name they appear in; nor is
        a value that is ``{user}`` alone: the user of a template (``postgres``)
        is no secret, and masking it would hide it everywhere in the output.
        """
        rendered = self.render_env(credentials)
        found = [credentials.password]
        found += [
            rendered[name]
            for name, template in self.env.items()
            if {"password", "user"} & _placeholders(template) and template.strip() != "{user}"
        ]
        return tuple(dict.fromkeys(v for v in found if v))


@dataclass(frozen=True)
class Check:
    name: str
    run: str
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunnerConfig:
    version: int
    neighbours: tuple[str, ...] = ()
    setup: str | None = None
    services: Mapping[str, ServiceSpec] = field(default_factory=dict)
    checks: tuple[Check, ...] = ()


@cache
def runner_config_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    return schema


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(runner_config_schema())


@cache
def _pattern(definition: str) -> re.Pattern[str]:
    return re.compile(runner_config_schema()["$defs"][definition]["pattern"])


def _matches(definition: str, value: str) -> bool:
    """Whether ``value`` matches a pattern of ``$defs``, the way JSON Schema checks it."""
    return _pattern(definition).search(value) is not None


def is_reserved_env_name(name: str) -> bool:
    """Whether a run environment may not set ``name`` (:data:`RESERVED_ENV_NAMES`)."""
    return _matches("reservedEnvName", name)


def reserved_entry(name: str) -> str | None:
    """The entry of :data:`RESERVED_ENV_NAMES` that ``name`` falls under, if any."""
    for entry in RESERVED_ENV_NAMES:
        candidate = name.upper() if entry in ANY_CASE_ENV_NAMES else name
        if entry.startswith("*"):
            matched = candidate.endswith(entry[1:])
        elif entry.endswith("*"):
            matched = candidate.startswith(entry[:-1])
        else:
            matched = candidate == entry
        if matched:
            return entry
    return None


def _reserved_message(name: Any) -> str:
    entry = reserved_entry(name) if isinstance(name, str) else None
    return f"reserved variable name ({entry or ' '.join(RESERVED_ENV_NAMES)})"


def run_environment(env: Mapping[str, str] | None) -> dict[str, str]:
    """What of ``env`` may reach the executor process of one run, as a fresh dict.

    Adapters call it on the environment the daemon hands them for a run (the
    ``env`` of the run's services, placeholders filled): the parser already
    refused reserved and malformed names, and this is the second line, for an
    environment that reached the adapter some other way. Dropped names are
    logged; values never are, they carry credentials.
    """
    allowed: dict[str, str] = {}
    for name, value in (env or {}).items():
        if not isinstance(name, str) or not _matches("envName", name):
            logger.warning("run environment: dropped malformed name %r", name)
        elif is_reserved_env_name(name):
            logger.warning("run environment: dropped reserved name %s", name)
        elif not isinstance(value, str):
            logger.warning("run environment: dropped %s, its value is not a string", name)
        elif "\x00" in value:
            # No process environment holds a NUL: kept, it would fail the start
            # of the executor instead of costing one variable.
            logger.warning("run environment: dropped %s, its value contains NUL", name)
        else:
            allowed[name] = value
    return allowed


def _name_rule(schema: Any) -> str | None:
    """The rule in words of the ``$defs`` name definition ``schema``, if it is one.

    Matched by identity, not by pattern: a repository key and a template name
    have the same pattern and different rules.
    """
    definitions = runner_config_schema()["$defs"]
    for definition, rule in NAME_RULES.items():
        if definitions[definition] is schema:
            return rule
    return None


def field_path(parts: Any) -> str:
    """``$.services.db.env.URL``, ``$.checks[0].run``, ``$.services["a b"]``."""
    path = ROOT
    for part in parts:
        if isinstance(part, int) and not isinstance(part, bool):
            path += f"[{part}]"
        elif isinstance(part, str) and _PLAIN_PART.fullmatch(part):
            path += f".{part}"
        else:
            path += f"[{json.dumps(part if isinstance(part, str) else repr(part))}]"
    return path


def _key_errors(node: Any, at: list[Any]) -> list[FieldError]:
    """YAML allows ``123:`` or ``null:`` as a key; JSON Schema never checks those."""
    errors: list[FieldError] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                errors.append(FieldError(field_path([*at, key]), "a key must be a string"))
            else:
                errors += _key_errors(value, [*at, key])
    elif isinstance(node, list):
        for index, item in enumerate(node):
            errors += _key_errors(item, [*at, index])
    return errors


def _schema_errors(document: Any) -> list[FieldError]:
    errors: list[FieldError] = []
    for error in _validator().iter_errors(document):
        at = list(error.absolute_path)
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            known = error.schema.get("properties", {})
            for name in error.instance:
                if name not in known:
                    errors.append(FieldError(field_path([*at, name]), "unknown field"))
        elif error.validator == "required" and isinstance(error.instance, dict):
            for name in error.validator_value:
                if name not in error.instance:
                    errors.append(FieldError(field_path([*at, name]), "required field is missing"))
        elif at == ["version"]:
            errors.append(FieldError(field_path(at), _UNSUPPORTED_VERSION))
        elif "propertyNames" in error.relative_schema_path:
            errors.append(FieldError(field_path([*at, error.instance]), _message_of(error)))
        else:
            errors.append(FieldError(field_path(at), _message_of(error)))
    return errors


def _message_of(error: Any) -> str:
    if error.validator == "not" and "propertyNames" in error.relative_schema_path:
        return _reserved_message(error.instance)
    if error.validator == "pattern":
        rule = _name_rule(error.schema)
        if rule is not None:
            return f"{error.instance!r} is not {rule}"
    return str(error.message)


def _placeholders(value: str) -> set[str]:
    """The placeholders an ``env`` value refers to; ``{{`` is no placeholder."""
    return {name for _text, name, _spec, _conv in string.Formatter().parse(value) if name}


def placeholder_errors(value: str) -> list[str]:
    """What is wrong with the placeholders of one ``env`` value, if anything."""
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError as exc:
        return [f"{exc}; write {{{{ and }}}} for literal braces"]
    problems = []
    for _text, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in PLACEHOLDERS or spec or conversion:
            shown = "{" + name + (f"!{conversion}" if conversion else "")
            shown += (f":{spec}" if spec else "") + "}"
            problems.append(
                f"unknown placeholder {shown}; allowed: "
                + " ".join("{" + p + "}" for p in PLACEHOLDERS)
            )
    return problems


def _semantic_errors(document: dict[str, Any]) -> list[FieldError]:
    """What a schema cannot say: references between fields and placeholders."""
    errors: list[FieldError] = []
    if not isinstance(document["version"], int):
        # JSON Schema takes 1.0 for an integer; YAML means a float by it.
        errors.append(FieldError(field_path(["version"]), _UNSUPPORTED_VERSION))
    services: dict[str, Any] = document.get("services") or {}
    owner_of_variable: dict[str, str] = {}
    for service_name, service in services.items():
        for variable, value in (service.get("env") or {}).items():
            at = ["services", service_name, "env", variable]
            errors += [FieldError(field_path(at), p) for p in placeholder_errors(value)]
            other = owner_of_variable.setdefault(variable, service_name)
            if other != service_name:
                errors.append(
                    FieldError(field_path(at), f"variable is already set by service {other!r}")
                )
    seen_checks: set[str] = set()
    for index, check in enumerate(document.get("checks") or []):
        if check["name"] in seen_checks:
            errors.append(FieldError(field_path(["checks", index, "name"]), "duplicate check name"))
        seen_checks.add(check["name"])
        for position, name in enumerate(check.get("services") or []):
            if name not in services:
                errors.append(
                    FieldError(
                        field_path(["checks", index, "services", position]),
                        f"no service {name!r} in services",
                    )
                )
    return errors


def validate_runner_config(document: Any) -> list[FieldError]:
    """Every problem of a loaded document; empty when it is a valid ``runner.yaml``."""
    errors = _key_errors(document, []) or _schema_errors(document)
    if errors:
        # References are only checked on a document of the right shape.
        return sorted(set(errors), key=lambda e: (e.path, e.message))
    return _semantic_errors(document)


def parse_runner_config(text: str | bytes) -> RunnerConfig:
    """Parse the text of a ``runner.yaml``; :class:`RunnerConfigError` if it is invalid."""
    try:
        document = yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "malformed YAML"
        raise RunnerConfigError([FieldError(ROOT, f"not valid YAML{where}: {problem}")]) from exc
    if document is None:
        raise RunnerConfigError([FieldError(ROOT, "the file is empty")])
    errors = validate_runner_config(document)
    if errors:
        raise RunnerConfigError(errors)
    return RunnerConfig(
        version=document["version"],
        neighbours=tuple(document.get("neighbours") or ()),
        setup=document.get("setup"),
        services={
            name: ServiceSpec(template=spec["template"], env=dict(spec.get("env") or {}))
            for name, spec in (document.get("services") or {}).items()
        },
        checks=tuple(
            Check(name=c["name"], run=c["run"], services=tuple(c.get("services") or ()))
            for c in document.get("checks") or ()
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Check a ``runner.yaml``: silent and 0 when valid, errors by field path and 1 when not."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1 or args[:1] in (["-h"], ["--help"]):
        print(f"usage: runner_config.py [{RUNNER_CONFIG_PATH}]", file=sys.stderr)
        return 2
    path = Path(args[0] if args else RUNNER_CONFIG_PATH)
    try:
        text = path.read_bytes()
    except OSError as exc:
        print(f"{path}: cannot read: {exc.strerror or exc}", file=sys.stderr)
        return 2
    try:
        parse_runner_config(text)
    except RunnerConfigError as exc:
        for error in exc.errors:
            print(f"{path}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
