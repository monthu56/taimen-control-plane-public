"""Install a catalog package fixture through the public API.

The files follow the superproject's package format (TAI-ADR-0044): one object
per file, ``{apiVersion, kind, key, spec}``, where ``spec`` is exactly the
body of the core's create request without its identity field, and ``${NAME}``
in a string is a variable of the installation. Only creation is done — a
test tenant starts empty — and only through the API, so a package that needs
anything core does not offer fails here, not in a special code path.
"""

import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests.helpers import auth

PACKAGES = Path(__file__).parent / "fixtures" / "packages"

# kind -> (create endpoint, the request field that carries the file's key);
# the order is the order of installation: what is referred to comes first.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "ArtifactType": ("/api/v1/artifact-types", "key"),
    "Role": ("/api/v1/roles", "slug"),
    "Skill": ("/api/v1/skills", "name"),
    "TaskType": ("/api/v1/task-types", "key"),
    "WorkRule": ("/api/v1/rules", "key"),
}
VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def load_package(name: str) -> list[dict[str, Any]]:
    """The objects of package ``name``, ``package.yaml`` and the tests excluded."""
    objects: list[dict[str, Any]] = []
    root = PACKAGES / name
    for path in sorted(root.rglob("*.yaml")):
        if path.relative_to(root).parts[0] == "tests":
            continue  # tests/*.test.yaml: run by POST /packages:test, not installed
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document["apiVersion"].endswith("/v1"), path
        if document["kind"] == "Package":
            continue
        assert document["kind"] in ENDPOINTS, f"{path}: unknown kind {document['kind']}"
        objects.append(document)
    return objects


def _substitute(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):

        def variable(match: re.Match[str]) -> str:
            assert match.group(1) in variables, f"installation variable {match.group(0)} unset"
            return variables[match.group(1)]

        return VARIABLE.sub(variable, value)
    if isinstance(value, list):
        return [_substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, variables) for key, item in value.items()}
    return value


async def install_package(
    client: httpx.AsyncClient,
    key: str,
    name: str,
    *,
    variables: Mapping[str, str] | None = None,
    kinds: Collection[str] = tuple(ENDPOINTS),
) -> dict[str, dict[str, Any]]:
    """Create the objects of ``kinds``; returns the created bodies by ``Kind/key``."""
    objects = load_package(name)
    created: dict[str, dict[str, Any]] = {}
    for kind in ENDPOINTS:
        if kind not in kinds:
            continue
        endpoint, identity = ENDPOINTS[kind]
        for document in (o for o in objects if o["kind"] == kind):
            body = {identity: document["key"], **_substitute(document["spec"], variables or {})}
            response = await client.post(endpoint, json=body, headers=auth(key))
            assert response.status_code == 201, f"{kind}/{document['key']}: {response.text}"
            created[f"{kind}/{document['key']}"] = response.json()
    return created
