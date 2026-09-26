"""``arith.double@1`` — a deterministic local skill for executor tests."""

import os
import time
from typing import Any

CONTRACT: dict[str, Any] = {
    "inputs": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "sleep": {"type": "number"}},
        "required": ["n"],
    },
    "outputs": {
        "type": "object",
        "properties": {"double": {"type": "integer"}},
        "required": ["double"],
    },
    "timeoutSeconds": 5,
    "implementation": {"protocol": "local", "entrypoint": "tests.skill_stubs.arith:run"},
}


class Flaky(Exception):
    retryable = True
    code = "upstream_busy"


def run(inputs: dict[str, Any]) -> dict[str, Any]:
    if inputs.get("sleep"):
        time.sleep(float(inputs["sleep"]))
    return {"double": inputs["n"] * 2}


def flaky(inputs: dict[str, Any]) -> dict[str, Any]:
    raise Flaky("the upstream is busy")


def broken(inputs: dict[str, Any]) -> dict[str, Any]:
    raise ValueError("cannot do that")


def wrong_shape(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"double": "four"}


async def async_run(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"double": inputs["n"] * 2}


def mark(inputs: dict[str, Any]) -> dict[str, Any]:
    """Sleep, then leave a file behind: proves whether an abandoned call ran on."""
    time.sleep(float(inputs.get("sleep") or 0))
    with open(inputs["marker"], "w") as handle:
        handle.write("ran")
    return {"double": inputs["n"] * 2}


def crash(inputs: dict[str, Any]) -> dict[str, Any]:
    os._exit(3)
