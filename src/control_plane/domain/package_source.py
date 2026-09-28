"""A package as its files: parsed by the core, every finding placed in a file and line.

``PackageSource {files: [{path, content}]}`` (CP-ADR-0074 §10) is the package
directory as its author has it: ``package.yaml``, catalog objects in the
envelope ``{apiVersion, kind, key, spec}`` in any other ``*.yaml``, tests in
``tests/*.test.yaml``, data schemas and other files a process refers to.

- **YAML 1.2.** ``on``, ``off``, ``yes``, ``no`` are strings: the key ``on``
  of a trigger stays ``on`` (TAI-ADR-0054 p.11). The superproject's
  ``cp_packages`` reads packages the same way.
- **Places.** Each document keeps a map JSON pointer → line, so a finding of
  the check of a process (``/spec/stages/0/steps/1``) names the line of the
  file; a pointer the file does not have takes the line of its nearest parent.
- **``data: {$ref: <file>}``** of a process is inlined from the package
  (JSON or YAML, a path relative to the process file, never outside the
  package), as ``cp_packages`` does before it publishes.
- **Tests** are checked against ``packages/schema/v1/test.schema.json``; the
  copy the core holds (``package_test.schema.json``) is kept equal to the
  superproject by a contract test.

What is found here is a finding like those of the check of a definition
(:class:`Problem`): ``invalid_yaml``, ``invalid_document``, ``unknown_kind``,
``duplicate_object``, ``unresolved_data_ref``, ``invalid_test``,
``unknown_test_process``.

Pure functions over plain values; no I/O.
"""

import json
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jsonschema import Draft202012Validator

from control_plane.domain.process_definition import Problem, pointer

# The version of the catalog format the core reads: ``apiVersion`` is
# ``<catalog>/v1``; which catalog is the package tool's check (cp_packages).
FORMAT_VERSION = "v1"
# Kinds of the catalog schema (packages/schema/v1/object.schema.json).
KINDS = (
    "Package",
    "Installation",
    "ArtifactType",
    "TaskType",
    "ProjectTemplate",
    "WorkspaceType",
    "Role",
    "Capability",
    "Skill",
    "WorkRule",
    "Agent",
    "NotificationRule",
    "Process",
    "Calendar",
)
MANIFEST = "package.yaml"
TESTS_DIR = "tests"
TEST_SUFFIXES = (".test.yaml", ".test.yml")
# Directories that hold no catalog objects: schemas a process refers to, the
# layout of the visual editor.
RESOURCE_DIRS = ("schemas", ".layout")
YAML_SUFFIXES = (".yaml", ".yml")
TEST_SCHEMA_FILE = Path(__file__).with_name("package_test.schema.json")
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_TEST_PROBLEMS = 20

Locate = Callable[[str], int | None]


# --- YAML 1.2 with places -----------------------------------------------------------------


@cache
def yaml12_loader() -> type[yaml.SafeLoader]:
    """SafeLoader with the booleans of YAML 1.2 only: ``true`` and ``false``."""

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    return Loader


class SourceError(ValueError):
    """A file is not YAML (or JSON); ``line`` is 1-based when known."""

    def __init__(self, message: str, line: int | None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


def _lines(node: yaml.Node, path: str, out: dict[str, int]) -> None:
    out.setdefault(path, node.start_mark.line + 1)
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            name = pointer(str(key.value)) if isinstance(key, yaml.ScalarNode) else "/?"
            out.setdefault(path + name, key.start_mark.line + 1)
            _lines(value, path + name, out)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _lines(item, f"{path}/{index}", out)


def load_yaml(text: str) -> tuple[Any, dict[str, int]]:
    """The document and its lines by JSON pointer; an empty file is ``None``."""
    loader = yaml12_loader()(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None, {}
        lines: dict[str, int] = {}
        _lines(node, "", lines)
        return loader.construct_document(node), lines
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        message = " ".join(str(part) for part in (exc.context, exc.problem) if part)
        raise SourceError(message or "not YAML", mark.line + 1 if mark else None) from None
    except yaml.YAMLError as exc:
        raise SourceError(str(exc), None) from None
    finally:
        loader.dispose()


def load_file(path: str, text: str) -> tuple[Any, dict[str, int]]:
    """A YAML or JSON file of the package (JSON is YAML, but its errors read better)."""
    if path.endswith(".json"):
        try:
            return json.loads(text), {}
        except json.JSONDecodeError as exc:
            raise SourceError(exc.msg, exc.lineno) from None
    return load_yaml(text)


def locator(lines: Mapping[str, int]) -> Locate:
    return lambda path: lines.get(path)


# --- the package -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageObject:
    """A catalog object of the package: its envelope unwrapped."""

    kind: str
    key: str
    spec: dict[str, Any]
    file: str
    lines: Mapping[str, int] = field(default_factory=dict, compare=False)

    @property
    def locate(self) -> Locate:
        return locator(self.lines)

    @property
    def ref(self) -> str:
        """``kind/key``; a skill is ``Skill/<name>@<version>``: its versions are objects."""
        if self.kind == "Skill":
            return f"Skill/{self.key}@{self.spec.get('version')}"
        return f"{self.kind}/{self.key}"

    def place(self, problem: Problem) -> Problem:
        return placed(problem, self.file, self.locate)


@dataclass(frozen=True)
class PackageTestFile:
    """A test of the package (``tests/<name>.test.yaml``), checked against the test schema."""

    file: str
    data: dict[str, Any]
    lines: Mapping[str, int] = field(default_factory=dict, compare=False)

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.file)

    @property
    def process(self) -> str:
        return str(self.data.get("process") or "")

    def locate(self, path: str) -> int | None:
        return self.lines.get(path)


@dataclass
class ParsedPackage:
    manifest: dict[str, Any] | None = None
    # The Package object of package.yaml: its key and places (renames).
    manifest_object: PackageObject | None = None
    objects: list[PackageObject] = field(default_factory=list)
    tests: list[PackageTestFile] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[PackageObject]:
        return [obj for obj in self.objects if obj.kind == kind]

    def process(self, key: str) -> PackageObject | None:
        return next((obj for obj in self.of_kind("Process") if obj.key == key), None)


def placed(problem: Problem, file: str | None, locate: Locate | None) -> Problem:
    """``problem`` in ``file`` at the line of its path, or of the nearest parent the file has."""
    line = problem.line
    if line is None and locate is not None:
        path = problem.path
        while True:
            line = locate(path)
            if line is not None or not path:
                break
            path = path.rsplit("/", 1)[0]
    return Problem(
        problem.code, problem.severity, problem.path, problem.message, problem.hint, file, line
    )


def _error(
    code: str, path: str, message: str, file: str, line: int | None, hint: str | None = None
) -> Problem:
    return Problem(code, "error", path, message, hint, file, line)


def _is_test(path: str) -> bool:
    return path.split("/", 1)[0] == TESTS_DIR and path.endswith(TEST_SUFFIXES)


def _is_object(path: str) -> bool:
    first = path.split("/", 1)[0]
    return (
        path.endswith(YAML_SUFFIXES)
        and path != MANIFEST
        and first not in RESOURCE_DIRS
        and first != TESTS_DIR
    )


def parse_package(files: Iterable[tuple[str, str]]) -> ParsedPackage:
    """The objects and tests of a package from its files ``(path, content)``, with findings."""
    contents = dict(files)
    package = ParsedPackage()
    seen: dict[str, str] = {}
    for path in sorted(contents):
        if not (path == MANIFEST or _is_object(path) or _is_test(path)):
            continue
        try:
            document, lines = load_yaml(contents[path])
        except SourceError as exc:
            package.problems.append(_error("invalid_yaml", "", exc.message, path, exc.line))
            continue
        if _is_test(path):
            _add_test(package, path, document, lines)
            continue
        obj = _envelope(package, path, document, lines)
        if obj is None:
            continue
        if path == MANIFEST:
            if obj.kind != "Package":
                package.problems.append(
                    _error(
                        "invalid_document",
                        "/kind",
                        f"{MANIFEST} holds the object of kind Package, not {obj.kind}",
                        path,
                        lines.get("/kind"),
                    )
                )
            else:
                package.manifest = obj.spec
                package.manifest_object = obj
            continue
        if obj.kind == "Process":
            obj = _inline_data(package, obj, contents)
        if obj.ref in seen:
            package.problems.append(
                _error(
                    "duplicate_object",
                    "/key",
                    f"{obj.ref} is also defined in {seen[obj.ref]}",
                    path,
                    lines.get("/key"),
                )
            )
            continue
        seen[obj.ref] = path
        package.objects.append(obj)
    processes = {obj.key for obj in package.of_kind("Process")}
    for test in package.tests:
        if test.process not in processes:
            package.problems.append(
                _error(
                    "unknown_test_process",
                    "/process",
                    f"the package has no process {test.process!r}",
                    test.file,
                    test.locate("/process"),
                    hint=_known(processes),
                )
            )
    return package


def _known(names: Iterable[str]) -> str | None:
    listed = sorted(names)
    return f"processes of the package: {', '.join(listed)}" if listed else None


def _envelope(
    package: ParsedPackage, path: str, document: Any, lines: dict[str, int]
) -> PackageObject | None:
    def refuse(where: str, message: str, hint: str | None = None) -> None:
        package.problems.append(
            _error("invalid_document", where, message, path, lines.get(where, 1), hint)
        )

    if not isinstance(document, dict):
        refuse("", "a catalog object is a mapping {apiVersion, kind, key, spec}")
        return None
    api_version = document.get("apiVersion")
    if not isinstance(api_version, str) or api_version.rsplit("/", 1)[-1] != FORMAT_VERSION:
        refuse("/apiVersion", f"apiVersion names version {FORMAT_VERSION} of the catalog format")
        return None
    kind, key, spec = document.get("kind"), document.get("key"), document.get("spec")
    if kind not in KINDS:
        package.problems.append(
            _error(
                "unknown_kind",
                "/kind",
                f"unknown kind {kind!r}",
                path,
                lines.get("/kind"),
                hint=f"one of {', '.join(KINDS)}",
            )
        )
        return None
    if not isinstance(key, str) or not key:
        refuse("/key", "key is a non-empty string")
        return None
    if kind in ("Process", "Calendar") and not KEY_PATTERN.match(key):
        refuse("/key", f"the key of a {kind} matches {KEY_PATTERN.pattern}")
        return None
    if not isinstance(spec, dict):
        refuse("/spec", "spec is a mapping")
        return None
    return PackageObject(str(kind), key, spec, path, lines)


def _inline_data(
    package: ParsedPackage, obj: PackageObject, contents: Mapping[str, str]
) -> PackageObject:
    data = obj.spec.get("data")
    if not (isinstance(data, dict) and set(data) == {"$ref"} and isinstance(data["$ref"], str)):
        return obj
    ref = data["$ref"]
    if ref.startswith("#") or "://" in ref:
        return obj  # inside the document or remote: the check of the definition refuses it
    where = "/spec/data/$ref"

    def refuse(message: str) -> PackageObject:
        package.problems.append(
            _error(
                "unresolved_data_ref",
                where,
                message,
                obj.file,
                obj.lines.get("/spec/data/$ref") or obj.lines.get("/spec/data"),
                hint="a path relative to the process file, inside the package",
            )
        )
        return obj

    target = posixpath.normpath(posixpath.join(posixpath.dirname(obj.file), ref))
    if target.startswith("../") or target == ".." or target.startswith("/"):
        return refuse(f"$ref {ref!r} leads outside the package")
    if target not in contents:
        return refuse(f"$ref {ref!r}: the package has no file {target}")
    try:
        schema, _ = load_file(target, contents[target])
    except SourceError as exc:
        return refuse(f"$ref {ref!r}: {target} is not YAML or JSON: {exc.message}")
    if not isinstance(schema, dict):
        return refuse(f"$ref {ref!r}: {target} is not a JSON Schema object")
    return PackageObject(obj.kind, obj.key, {**obj.spec, "data": schema}, obj.file, obj.lines)


# --- tests ----------------------------------------------------------------------------------


@cache
def package_test_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(TEST_SCHEMA_FILE.read_text(encoding="utf-8"))
    return schema


@cache
def _test_validator() -> Draft202012Validator:
    return Draft202012Validator(
        package_test_schema(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def _deepest(error: jsonschema.ValidationError) -> jsonschema.ValidationError:
    if not error.context:
        return error
    best = jsonschema.exceptions.best_match(error.context)
    return _deepest(best) if len(best.absolute_path) >= len(error.absolute_path) else error


def _add_test(package: ParsedPackage, path: str, document: Any, lines: dict[str, int]) -> None:
    errors = sorted(_test_validator().iter_errors(document), key=lambda e: list(e.absolute_path))
    for error in errors[:MAX_TEST_PROBLEMS]:
        cause = _deepest(error)
        where = pointer(*cause.absolute_path)
        package.problems.append(
            placed(
                Problem("invalid_test", "error", where, cause.message[:500]),
                path,
                locator(lines),
            )
        )
    if errors:
        return
    package.tests.append(PackageTestFile(path, document, lines))


def select_tests(
    package: ParsedPackage, wanted: Sequence[str] | None
) -> tuple[list[PackageTestFile], list[Problem]]:
    """The tests the request names (all by default); a named file that is no test is a finding."""
    if wanted is None:
        return list(package.tests), []
    by_file = {test.file: test for test in package.tests}
    chosen: list[PackageTestFile] = []
    problems: list[Problem] = []
    for index, name in enumerate(dict.fromkeys(wanted)):
        test = by_file.get(name)
        if test is not None:
            chosen.append(test)
            continue
        if not any(problem.file == name for problem in package.problems):
            problems.append(
                Problem(
                    "unknown_test",
                    "error",
                    pointer("tests", index),
                    f"the package has no test file {name}",
                    hint=f"tests are files {TESTS_DIR}/<name>.test.yaml",
                )
            )
    return chosen, problems
