"""``GET /rule-evaluations/{id}`` in the contract (CP-ADR-0063, amendment 2026-09-29).

The route is served and answers the same ``RuleEvaluationOut`` as an item of
``/rules/{id}/evaluations``.
"""

from typing import Any

from fastapi import FastAPI

from control_plane.api.v1.router import api_v1_router


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


def test_openapi_carries_the_route_of_one_evaluation() -> None:
    paths = _openapi()["paths"]
    route = paths["/api/v1/rule-evaluations/{evaluation_id}"]
    assert set(route) == {"get"}
    ok = route["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/RuleEvaluationOut"}
    assert {"403", "404"} <= set(route["get"]["responses"])
    parameter = next(p for p in route["get"]["parameters"] if p["name"] == "evaluation_id")
    assert (parameter["name"], parameter["in"]) == ("evaluation_id", "path")
    assert parameter["schema"]["format"] == "uuid"
