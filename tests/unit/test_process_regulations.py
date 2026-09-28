"""``governedBy`` resolved through memory in the check of a package (CP-ADR-0076 §7).

Memory here is ``FakeGraphMemory``: every typed read is validated against the
pinned Memory contract, and anchors resolve the way memory resolves them.
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.application.context.graph import GraphScope
from control_plane.application.queries import process_regulations
from control_plane.config import Settings
from control_plane.domain.process_definition import (
    GOVERNED_BY_UNCHECKED,
    UNKNOWN_DOCUMENT,
    governed_references,
)
from tests.fake_graph_memory import FakeGraphMemory, Node
from tests.unit.test_process_contract import _yaml12_loader

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
SAMPLE: dict[str, Any] = yaml.load(
    (FIXTURES / "sample.process.yaml").read_text("utf-8"), Loader=_yaml12_loader()
)
SCOPE = GraphScope(namespace="tenant:t", namespaces=["tenant:t", "tenant:t:ws:r"])
SETTINGS = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d")  # type: ignore[call-arg]


def _spec() -> dict[str, Any]:
    spec = copy.deepcopy(SAMPLE["spec"])
    spec["stages"][0]["steps"][2]["governedBy"] = [
        {"document": "regulation:procurement", "section": "5.2"},
        {"document": "regulation:missing"},
    ]
    spec["decisions"][0]["rules"][0]["governedBy"] = [{"document": "regulation:missing"}]
    # A data field that happens to be called governedBy is not a reference.
    spec["data"]["properties"]["governedBy"] = {"type": "array"}
    return spec


def _memory(*documents: str) -> FakeGraphMemory:
    memory = FakeGraphMemory()
    for key in documents:
        memory.nodes[key] = Node(key, "regulation", aliases=(f"alias:{key}",))
    return memory


def test_references_are_every_governed_by_of_the_spec() -> None:
    assert governed_references(_spec()) == [
        ("regulation:sample", "/spec/governedBy/0/document"),
        ("regulation:missing", "/spec/decisions/0/rules/0/governedBy/0/document"),
        ("regulation:procurement", "/spec/stages/0/steps/2/governedBy/0/document"),
        ("regulation:missing", "/spec/stages/0/steps/2/governedBy/1/document"),
    ]


@pytest.mark.anyio
async def test_a_missing_document_is_a_warning_at_each_reference() -> None:
    memory = _memory("regulation:sample", "regulation:procurement")
    problems = await process_regulations.governed_by_problems(memory, SCOPE, _spec(), SETTINGS)
    assert [(p.code, p.severity, p.path) for p in problems] == [
        (UNKNOWN_DOCUMENT, "warning", "/spec/decisions/0/rules/0/governedBy/0/document"),
        (UNKNOWN_DOCUMENT, "warning", "/spec/stages/0/steps/2/governedBy/1/document"),
    ]
    assert "regulation:missing" in problems[0].message
    # One read, the documents once each, nothing traversed, nothing by similarity.
    [request] = memory.typed_requests
    assert [a["value"] for a in request["anchors"]] == [
        "regulation:sample",
        "regulation:missing",
        "regulation:procurement",
    ]
    assert request["traverse"] == [] and request["allow_semantic"] is False


@pytest.mark.anyio
async def test_every_document_known_is_no_warning() -> None:
    memory = _memory("regulation:sample", "regulation:procurement", "regulation:missing")
    assert await process_regulations.governed_by_problems(memory, SCOPE, _spec(), SETTINGS) == []


@pytest.mark.anyio
async def test_many_documents_are_read_in_batches_of_memorys_anchor_limit() -> None:
    spec = copy.deepcopy(SAMPLE["spec"])
    spec["governedBy"] = [{"document": f"regulation:{i}"} for i in range(45)]
    memory = _memory(*(f"regulation:{i}" for i in range(44)))
    problems = await process_regulations.governed_by_problems(memory, SCOPE, spec, SETTINGS)
    assert [p.path for p in problems] == ["/spec/governedBy/44/document"]
    assert [len(r["anchors"]) for r in memory.typed_requests] == [20, 20, 5]


def test_only_an_exact_match_names_the_document() -> None:
    pack = {
        "anchors": [
            {"input": {"value": "a"}, "resolved": [{}], "matchedBy": "natural_key"},
            {"input": {"value": "b"}, "resolved": [{}], "matchedBy": "alias"},
            {"input": {"value": "c"}, "resolved": [{}], "matchedBy": "suffix"},
            {"input": {"value": "d"}, "resolved": [], "matchedBy": None},
        ]
    }
    assert process_regulations._known(pack) == {"a", "b"}


@pytest.mark.anyio
async def test_memory_away_is_one_unchecked_warning() -> None:
    for memory in (None, FakeGraphMemory(fail="typed")):
        problems = await process_regulations.governed_by_problems(memory, SCOPE, _spec(), SETTINGS)
        assert [(p.code, p.severity, p.path) for p in problems] == [
            (GOVERNED_BY_UNCHECKED, "warning", "/spec")
        ]


@pytest.mark.anyio
async def test_a_process_without_regulations_does_not_ask_memory() -> None:
    spec = copy.deepcopy(SAMPLE["spec"])
    spec.pop("governedBy")
    memory = FakeGraphMemory()
    assert await process_regulations.governed_by_problems(memory, SCOPE, spec, SETTINGS) == []
    assert memory.typed_requests == []
