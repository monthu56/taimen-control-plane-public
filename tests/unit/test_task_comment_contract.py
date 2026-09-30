"""The author of a comment in the contract (ADR-0050, amendment of 2026-09-30).

Every body of a comment — created, edited, one, a page of the thread — names
its author by kind and display name, so a reader with ``tasks.read`` alone
tells the owner from an agent (TASK-001132).
"""

from typing import Any

import pytest
from fastapi import FastAPI

from control_plane.api.v1.router import api_v1_router


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(api_v1_router)
    return app.openapi()


OPENAPI = _openapi()
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]
PATHS: dict[str, Any] = OPENAPI["paths"]


def test_a_comment_carries_its_author_in_words() -> None:
    comment = SCHEMAS["TaskCommentOut"]
    assert "author" in comment["required"]
    assert "authorPrincipalId" in comment["required"]
    assert comment["properties"]["author"] == {"$ref": "#/components/schemas/TaskCommentAuthorOut"}
    author = SCHEMAS["TaskCommentAuthorOut"]
    assert set(author["properties"]) == {"kind", "displayName"}
    assert set(author["required"]) == {"kind", "displayName"}


@pytest.mark.parametrize(
    ("path", "method", "status"),
    [
        ("/api/v1/tasks/{task_ref}/comments", "post", "201"),
        ("/api/v1/tasks/{task_ref}/comments/{comment_id}", "get", "200"),
        ("/api/v1/tasks/{task_ref}/comments/{comment_id}", "patch", "200"),
    ],
)
def test_every_comment_body_is_the_one_with_the_author(path: str, method: str, status: str) -> None:
    content = PATHS[path][method]["responses"][status]["content"]["application/json"]
    assert content["schema"] == {"$ref": "#/components/schemas/TaskCommentOut"}
