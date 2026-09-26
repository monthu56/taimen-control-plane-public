"""The core's graph reads against Memory's pinned contract (CP-ADR-0064).

``tests/fixtures/memory_graph_contract.json`` holds what memory-service
publishes for ``POST /api/memory/context/typed`` (``ContextIn`` from its
``app.openapi()`` plus the body rules of ``TypedContextRequest.from_payload``)
and the answers of its pack registry for the software-delivery pack. The
bodies here are produced by ``HttpContextProvider`` from the same requests
the task context pack and ``cp_recall`` build, captured on the wire.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import regex
from jsonschema import Draft202012Validator

from control_plane.application.context import graph
from control_plane.domain.context_schema import (
    anchor_candidates,
    extract_identifiers,
    parse_context_schema,
)
from control_plane.infrastructure.context_provider.base import ContextProviderError
from control_plane.infrastructure.context_provider.http import HttpContextProvider

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_graph_contract.json").read_text()
)
NS = "tenant:t1:ws:w1"


def _provider(handler: Any) -> HttpContextProvider:
    return HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )


async def _capture(call: str, answer: Any = None, **kwargs: Any) -> tuple[httpx.Request, Any]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer if answer is not None else {})

    provider = _provider(handler)
    try:
        result = await getattr(provider, call)(**kwargs)
    finally:
        await provider.aclose()
    [request] = seen
    return request, result


@pytest.mark.anyio
async def test_typed_body_is_memorys_context_in() -> None:
    schema = parse_context_schema(
        {
            "anchors": [{"from": "description", "kinds": ["endpoint", "adr"]}],
            "traverse": [{"relation": "calls", "direction": "in"}],
        }
    )
    assert schema is not None
    patterns = {
        spec["kind"]: tuple(regex.compile(p) for p in spec.get("idPatterns") or [])
        for spec in CONTRACT["packages"]["software-delivery@1"]["kinds"]
    }
    anchors = anchor_candidates(
        schema, {"description": "POST /tasks/{task_id}:claim, CP-ADR-0058"}, patterns
    )
    request, _ = await _capture(
        "typed_context",
        namespace="tenant:t1",
        namespaces=["tenant:t1", NS],
        request={
            "anchors": [a.to_request() for a in anchors],
            "traverse": [s.to_request() for s in schema.traverse],
            "as_of": "2026-09-25T10:00:00+00:00",
            "allow_semantic": False,
            "allowedScopes": ["workspace:w1", "principal:p1"],
        },
    )
    path = "/api/memory/context/typed"
    assert request.method == "POST" and request.url.path == path
    assert CONTRACT["paths"][path]["post"]["requestBody"] == "ContextIn"
    body = json.loads(request.content)
    Draft202012Validator(CONTRACT["schemas"]["ContextIn"]).validate(body)
    Draft202012Validator(CONTRACT["typedRequest"]).validate(body)
    assert body["scope"] == {"namespace": "tenant:t1", "namespaces": ["tenant:t1", NS]}
    # Sent as written, once: Memory normalizes template parameters itself.
    assert body["anchors"][0] == {"kind": "endpoint", "value": "POST /tasks/{task_id}:claim"}
    assert {"kind": "endpoint", "value": "POST /tasks/{}:claim"} not in body["anchors"]
    # One namespace keeps the plain shape.
    single, _ = await _capture(
        "typed_context", namespace=NS, namespaces=[NS], request={"anchors": ["x"]}
    )
    assert json.loads(single.content)["scope"] == {"namespace": NS}


@pytest.mark.anyio
async def test_registry_reads_are_the_published_routes() -> None:
    kinds, answer = await _capture(
        "namespace_kinds", answer=CONTRACT["namespaceKinds"], namespace=NS
    )
    assert kinds.method == "GET" and kinds.content == b""
    assert kinds.url.raw_path.decode() == "/api/memory/namespaces/tenant%3At1%3Aws%3Aw1/kinds"
    assert CONTRACT["paths"]["/api/memory/namespaces/{namespace}/kinds"]["get"]["parameters"] == [
        "namespace"
    ]
    assert answer["catalog"]["packages"] == ["software-delivery@1"]

    package, _ = await _capture(
        "get_package",
        answer=CONTRACT["packages"]["software-delivery@1"],
        name="software-delivery",
        version="1",
    )
    assert package.method == "GET" and package.url.path == "/api/memory/packages/software-delivery"
    assert dict(package.url.params) == {"version": "1"}
    assert set(dict(package.url.params)) <= set(
        CONTRACT["paths"]["/api/memory/packages/{name}"]["get"]["parameters"]
    )


@pytest.mark.anyio
async def test_patterns_come_from_the_enabled_packs_and_are_cached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/kinds"):
            return httpx.Response(200, json=CONTRACT["namespaceKinds"])
        return httpx.Response(200, json=CONTRACT["packages"]["software-delivery@1"])

    graph._pack_patterns.clear()
    provider = _provider(handler)
    try:
        first = await graph.kind_patterns(provider, [NS])
        second = await graph.kind_patterns(provider, [NS])
    finally:
        await provider.aclose()
    # Only kinds that declare idPatterns can extract anything.
    assert set(first.patterns) == {"source_file", "endpoint", "event", "adr"}
    assert first == second
    # An immutable pack version is fetched once; the namespace setting each time.
    assert calls.count("/api/memory/packages/software-delivery") == 1
    assert calls.count(f"/api/memory/namespaces/{NS}/kinds") == 2


@pytest.mark.anyio
async def test_kind_aliases_of_a_pack_select_the_canonical_patterns() -> None:
    """``kindAliases`` (KindSpec.to_dict of memory-service) name a kind too: a
    profile saying ``route`` extracts with the patterns of ``endpoint``."""
    package = copy.deepcopy(CONTRACT["packages"]["software-delivery@1"])
    for spec in package["kinds"]:
        if spec["kind"] == "endpoint":
            spec["kindAliases"] = ["route"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/kinds"):
            return httpx.Response(200, json=CONTRACT["namespaceKinds"])
        return httpx.Response(200, json=package)

    graph._pack_patterns.clear()
    provider = _provider(handler)
    try:
        catalog = await graph.kind_patterns(provider, [NS])
    finally:
        await provider.aclose()
        graph._pack_patterns.clear()
    assert catalog.aliases == {"route": "endpoint"}
    found = extract_identifiers(
        "Fix POST /tasks/{task_id}:claim", catalog.patterns, ["route"], aliases=catalog.aliases
    )
    assert found == [("endpoint", "POST /tasks/{task_id}:claim")]


@pytest.mark.anyio
async def test_unreadable_catalog_is_a_warning_and_a_bad_pack_is_an_error() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "no"})

    provider = _provider(denied)
    warnings: list[str] = []
    try:
        assert await graph.kind_patterns(provider, [NS], warnings=warnings) == graph.KindPatterns()
    finally:
        await provider.aclose()
    assert warnings == ["kind catalog unavailable for a namespace (403)"]

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[1, 2]")

    provider = _provider(garbage)
    try:
        with pytest.raises(ContextProviderError) as caught:
            await provider.typed_context(namespace=NS, namespaces=[NS], request={"anchors": ["x"]})
    finally:
        await provider.aclose()
    assert caught.value.retryable is False


def test_pack_is_cut_to_the_budget_in_render_order() -> None:
    pack = {
        "sections": [
            {"kind": "endpoint", "items": [{"natural_key": "E" * 40} for _ in range(3)]},
            {"kind": "adr", "items": [{"natural_key": "A" * 40}]},
        ],
        "facts": [{"subject": "s", "relation": "calls", "object": "o"}],
        "used": {"entities": [1, 2, 3, 4], "facts": ["f"], "snapshots": []},
        "unresolved": [{"value": "x"}],
    }
    # 40 characters and the line overhead: two entities fit in 30 tokens.
    cut = graph.within_budget(pack, 30)
    assert [len(s["items"]) for s in cut["sections"]] == [2]
    assert cut["facts"] == []
    assert cut["omitted"] == {"entities": 2, "facts": 1}
    assert cut["used"] == pack["used"] and cut["unresolved"] == pack["unresolved"]
    whole = graph.within_budget(pack, 4000)
    assert "omitted" not in whole and whole["sections"] == pack["sections"]
