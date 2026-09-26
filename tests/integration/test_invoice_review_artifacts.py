"""Artifact-handoff on a second domain (CP-ADR-0072, spec scenario 5, SC-002).

The ``invoice-payment`` fixture package declares, as data only, an artifact
type (a review of an invoice, PDF), a task type that must hand one in, and
the payment task that takes it as an input from the task it depends on.
Scenarios 1, 3 and 4 of the spec run on it unchanged: the review hands in a
file (1), is not done without it (4), and the payment receives it in its run
context and can read it as its own input (3).

Core has no line of code for invoices: if a test here needed one, the
handoff would not be neutral.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from control_plane.config import Settings
from control_plane.infrastructure.content_store import InMemoryContentStore
from control_plane.worker.main import Worker
from tests.catalog_packages import install_package
from tests.helpers import auth, claim_task, create_task, do_bootstrap, open_session

PACKAGE = "invoice-payment"
PDF = b"%PDF-1.7\n% invoice review: amount and supplier match the contract\n%%EOF\n"


@pytest.fixture
def store(app: FastAPI) -> InMemoryContentStore:
    content_store = InMemoryContentStore()
    app.state.content_store = content_store
    return content_store


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    installed = await install_package(
        client, key, PACKAGE, kinds={"ArtifactType", "Skill", "TaskType"}
    )
    assert installed["ArtifactType/invoice-review"]["mediaTypes"] == ["application/pdf"]
    payment = installed["TaskType/invoice-payment"]["artifactSchema"]
    assert payment["inputs"][0]["from"] == "depends_on"
    return key


async def _task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _verify(
    client: httpx.AsyncClient, worker: Worker, key: str, ref: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = await _task(client, key, ref)
    response = await client.post(
        f"/api/v1/tasks/{ref}:complete",
        headers={**auth(key), "If-Match": f'"task-{current["version"]}"'},
    )
    assert response.status_code == 200, response.text
    await worker.run_once()
    attempts = await client.get(f"/api/v1/tasks/{ref}/verifications", headers=auth(key))
    assert attempts.status_code == 200, attempts.text
    return await _task(client, key, ref), attempts.json()["items"][0]


async def _hand_in(
    client: httpx.AsyncClient, key: str, task_id: str, body: bytes, media_type: str
) -> httpx.Response:
    upload = await client.put(
        "/api/v1/artifact-contents",
        content=body,
        headers={**auth(key), "Content-Type": media_type},
    )
    assert upload.status_code == 201, upload.text
    return await client.post(
        "/api/v1/artifacts",
        json={
            "task": task_id,
            "type": "invoice-review",
            "name": "review.pdf",
            "contentRef": upload.json()["contentRef"],
            "metadata": {"invoice": "INV-7"},
        },
        headers=auth(key),
    )


async def test_the_review_is_not_done_without_its_pdf(
    client: httpx.AsyncClient, worker: Worker, admin: str, store: InMemoryContentStore
) -> None:
    review = await create_task(client, admin, title="Check INV-7", typeKey="invoice-review")

    open_task, attempt = await _verify(client, worker, admin, review["id"])
    assert attempt["status"] == "failed"
    assert attempt["results"][0]["key"] == "output.review"
    assert attempt["results"][0]["reason"] == "artifact_missing"
    assert open_task["systemStatusCategory"] != "terminal_success"

    # The type admits a PDF only: a text note is not a review.
    wrong = await _hand_in(client, admin, review["id"], b"looks fine", "text/plain")
    assert wrong.status_code == 422, wrong.text

    handed_in = await _hand_in(client, admin, review["id"], PDF, "application/pdf")
    assert handed_in.status_code == 201, handed_in.text
    done, attempt = await _verify(client, worker, admin, review["id"])
    assert attempt["status"] == "passed"
    assert attempt["results"][0]["evidence"] == [
        {"kind": "artifact", "ref": handed_in.json()["id"]}
    ]
    assert done["systemStatusCategory"] == "terminal_success"


async def test_the_payment_receives_the_review_as_its_input(
    client: httpx.AsyncClient, worker: Worker, admin: str, store: InMemoryContentStore
) -> None:
    review = await create_task(client, admin, title="Check INV-7", typeKey="invoice-review")
    pdf = (await _hand_in(client, admin, review["id"], PDF, "application/pdf")).json()
    _, attempt = await _verify(client, worker, admin, review["id"])
    assert attempt["status"] == "passed"

    payment = await create_task(client, admin, title="Pay INV-7", typeKey="invoice-payment")
    relation = await client.post(
        f"/api/v1/tasks/{payment['id']}/relations",
        json={"toTask": review["id"], "type": "depends_on"},
        headers=auth(admin),
    )
    assert relation.status_code == 201, relation.text

    session = await open_session(client, admin)
    claim = await claim_task(client, admin, payment["id"], session["id"])
    assert claim.status_code == 200, claim.text
    run = await client.post(
        f"/api/v1/tasks/{payment['id']}:start-run",
        json={"claimId": claim.json()["id"], "fencingToken": claim.json()["fencingToken"]},
        headers=auth(admin),
    )
    assert run.status_code == 201, run.text
    context = await client.get(f"/api/v1/runs/{run.json()['id']}/context", headers=auth(admin))
    assert context.status_code == 200, context.text
    [review_input] = context.json()["inputs"]
    assert review_input["key"] == "review"
    assert review_input["artifactId"] == pdf["id"]
    assert review_input["mediaType"] == "application/pdf"
    assert review_input["contentState"] == "stored"
    assert review_input["sourceTask"]["relation"] == "depends_on"

    content = await client.get(
        f"/api/v1/artifacts/{pdf['id']}/content",
        params={"forTask": payment["id"]},
        headers=auth(admin),
    )
    assert content.status_code == 200, content.text
    assert content.content == PDF


async def test_a_payment_without_a_review_is_still_claimable(
    client: httpx.AsyncClient, admin: str, store: InMemoryContentStore
) -> None:
    """The input is optional: the rule files payments without a separate review."""
    payment = await create_task(client, admin, title="Pay INV-8", typeKey="invoice-payment")
    session = await open_session(client, admin)
    claim = await claim_task(client, admin, payment["id"], session["id"])
    assert claim.status_code == 200, claim.text
