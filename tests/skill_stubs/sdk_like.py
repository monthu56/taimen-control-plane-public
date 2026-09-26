"""A skill shaped like skill-sdk's (TAI-ADR-0045) without depending on it.

The executor knows the SDK only by its wire contract: ``__skill_contract__``
for discovery and ``__skill_invoke__(inputs, meta) -> {outputs, cost}`` for the
call. This stub implements exactly that.
"""

from typing import Any


class _SdkLikeSkill:
    def __init__(self, entrypoint: str) -> None:
        self.entrypoint = entrypoint

    @property
    def __skill_contract__(self) -> dict[str, Any]:
        return {"implementation": {"protocol": "local", "entrypoint": self.entrypoint}}

    def __skill_invoke__(self, inputs: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "outputs": {"double": inputs["n"] * 2, "seen": dict(meta)},
            "cost": {"units": {"ops": 1.0}},
        }

    def __call__(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return self.__skill_invoke__(inputs, {})["outputs"]


double = _SdkLikeSkill("tests.skill_stubs.sdk_like:double")
