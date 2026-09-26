"""``git.merge@1`` stand-in for the approval-outcome loop (CP-ADR-0061 §10).

Same inputs and outputs as the selfdev integration's contract; instead of
touching a repository it answers by the branch name: a branch ending in
``-conflict`` does not merge (``merged: false``, ``reason: conflict``) — a
contract answer, not an error.
"""

from typing import Any

ENTRYPOINT = "tests.skill_stubs.git_merge:run"

CONTRACT: dict[str, Any] = {
    "idempotency": "natural",
    "timeoutSeconds": 30,
    "inputs": {
        "type": "object",
        "required": ["repository", "branch", "commit", "target"],
        "additionalProperties": False,
        "properties": {
            "repository": {"type": "string", "minLength": 1},
            "branch": {"type": "string", "minLength": 1},
            "commit": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
            "target": {"type": "string", "minLength": 1},
            "message": {"type": "string"},
        },
    },
    "outputs": {
        "type": "object",
        "required": ["merged"],
        "properties": {
            "merged": {"type": "boolean"},
            "sha": {"type": "string"},
            "reason": {"enum": ["conflict", "branch_moved", "already_merged"]},
            "details": {"type": "string"},
        },
    },
    "implementation": {"protocol": "local", "entrypoint": ENTRYPOINT},
}


def run(inputs: dict[str, Any]) -> dict[str, Any]:
    if inputs["branch"].endswith("-conflict"):
        return {"merged": False, "reason": "conflict", "details": f"CONFLICT in {inputs['branch']}"}
    return {"merged": True, "sha": "f" * 40}
