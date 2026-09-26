"""``repo.merge@1`` — stands in for ``git.merge@1`` of the selfdev integration.

An ``external_write`` skill executed as Work (``task_types.execution``); it
records what it was asked to do instead of touching a repository.
"""

from typing import Any

MERGED: list[dict[str, Any]] = []

CONTRACT: dict[str, Any] = {
    "inputs": {
        "type": "object",
        "properties": {"branch": {"type": "string", "minLength": 1}, "into": {"type": "string"}},
        "required": ["branch", "into"],
        "additionalProperties": False,
    },
    "outputs": {
        "type": "object",
        "properties": {"merged": {"type": "string"}},
        "required": ["merged"],
    },
    "timeoutSeconds": 10,
    "idempotency": "natural",
    "implementation": {"protocol": "local", "entrypoint": "tests.skill_stubs.merge:run"},
}


def run(inputs: dict[str, Any]) -> dict[str, Any]:
    MERGED.append(dict(inputs))
    return {"merged": f"{inputs['branch']}->{inputs['into']}"}
