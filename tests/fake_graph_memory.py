"""A small knowledge graph behind the core's graph-read contract (CP-ADR-0064).

Not a stub that accepts anything: every request is validated against the
Memory Service contract pinned in ``tests/fixtures/memory_graph_contract.json``
(``ContextIn`` + the rules of ``TypedContextRequest.from_payload``), and the
answer is computed the way ``platform_memory.context.typed`` computes it —
anchors by key or alias, as written or with ``{name}`` normalized
(``resolve_candidates``), traversal only over edges valid at ``as_of``,
sections by kind, ``used`` with entities, facts and snapshot ids. The pack
registry answers are memory-service's own (``packages``/``namespaceKinds``).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from control_plane.infrastructure.context_provider.base import ContextProviderError, IngestResult

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "memory_graph_contract.json").read_text()
)
PACK_REF = "software-delivery@1"
# platform_memory.context.resolve: stored keys carry template parameters unnamed.
_PLACEHOLDER = re.compile(r"\{[^{}\s]*\}")


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@dataclass
class Node:
    key: str
    kind: str
    title: str = ""
    aliases: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    scopes: tuple[str, ...] = ()


@dataclass
class Edge:
    subject: str
    relation: str
    object: str
    valid_from: str = "2026-01-01T00:00:00+00:00"
    valid_to: str | None = None
    fact_id: str = ""
    snapshot_id: str = "snap-1"

    def valid_at(self, as_of: str | None) -> bool:
        if as_of is None:
            return self.valid_to is None
        at = _ts(as_of)
        start, end = _ts(self.valid_from), _ts(self.valid_to)
        assert at is not None and start is not None
        return start <= at and (end is None or at < end)


def software_delivery_graph() -> tuple[list[Node], list[Edge]]:
    """Who calls the claim endpoint, where it is defined, which ADR governs it."""
    endpoint = "POST /tasks/{}:claim"
    nodes = [
        Node(
            endpoint,
            "endpoint",
            "Claim a task",
            aliases=("/tasks/{task_id}:claim",),
            attributes={"method": "POST", "path": "/tasks/{task_id}:claim"},
            source_path="control-plane@abc123:src/control_plane/api/v1/claims.py:40",
        ),
        Node(
            "GET /runs/{}/checkpoints",
            "endpoint",
            "List checkpoints",
            aliases=("/runs/{run_id}/checkpoints",),
        ),
        Node(
            "control-plane:control_plane_client.client.ControlPlaneClient.claim_task",
            "client_method",
            source_path="control-plane@abc123:client/src/control_plane_client/client.py:700",
        ),
        Node(
            "platform-web:src/api/tasks.ts:42",
            "ui_call",
            source_path="platform-web@def456:src/api/tasks.ts:42",
        ),
        Node("platform-web:src/api/runs.ts:17", "ui_call"),
        Node("control-plane:src/control_plane/api/v1/claims.py", "source_file"),
        Node("CP-0019", "adr", "Claims and leases", aliases=("ADR-0019", "CP-ADR-0019")),
        # Visible only to one workspace: the core's narrowing must hide it.
        Node("secret-app:src/api.ts:1", "ui_call", scopes=("workspace:other",)),
    ]
    edges = [
        Edge(nodes[2].key, "calls", endpoint, fact_id="f-client-claim"),
        Edge(nodes[3].key, "calls", endpoint, fact_id="f-ui-claim"),
        Edge(nodes[4].key, "calls", "GET /runs/{}/checkpoints", fact_id="f-ui-checkpoints"),
        Edge(nodes[7].key, "calls", endpoint, fact_id="f-secret-claim"),
        Edge(endpoint, "defined_in", nodes[5].key, fact_id="f-claim-defined"),
        Edge("CP-0019", "governs", nodes[5].key, fact_id="f-adr-governs"),
        # A caller that stopped calling before any task here was created.
        Edge(
            "control-plane:legacy.claim",
            "calls",
            endpoint,
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-02-01T00:00:00+00:00",
            fact_id="f-legacy-claim",
        ),
    ]
    nodes.append(Node("control-plane:legacy.claim", "client_method"))
    return nodes, edges


class FakeGraphMemory:
    """Memory's ``/context``, ``/context/typed`` and pack registry, in process."""

    def __init__(self, *, fail: str | None = None) -> None:
        nodes, edges = software_delivery_graph()
        self.nodes = {n.key: n for n in nodes}
        self.edges = edges
        self.fail = fail  # None | "typed" | "kinds"
        self.typed_requests: list[dict[str, Any]] = []
        self.context_requests: list[dict[str, Any]] = []
        self.kind_requests: list[str] = []
        self.package_requests: list[tuple[str, str]] = []
        self._request_schema = Draft202012Validator(CONTRACT["typedRequest"])
        self._context_in = Draft202012Validator(CONTRACT["schemas"]["ContextIn"])

    # --- the /context recall half: not under test here ----------------------

    async def build_context(
        self,
        *,
        namespace: str,
        request: dict[str, Any],
        trace_run_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        self.context_requests.append({"namespace": namespace, **request})
        return {"sections": [], "sources": [], "trace_id": "ctx-recall"}

    async def ingest_batch(self, **_: Any) -> IngestResult:
        return IngestResult()

    async def healthy(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    # --- pack registry ----------------------------------------------------------

    async def namespace_kinds(
        self, *, namespace: str, trace_run_id: str | None = None
    ) -> dict[str, Any]:
        self.kind_requests.append(namespace)
        if self.fail == "kinds":
            raise ContextProviderError("no catalog", retryable=False, status=403)
        if ":ws:" not in namespace:
            # The tenant namespace enables no pack: only Memory's default.
            return {"settings": {"packages": None}, "catalog": {"packages": []}}
        return {**CONTRACT["namespaceKinds"], "settings": {"namespace": namespace}}

    async def get_package(
        self, *, name: str, version: str = "", trace_run_id: str | None = None
    ) -> dict[str, Any]:
        self.package_requests.append((name, version))
        return CONTRACT["packages"][f"{name}@{version}"]

    # --- typed traversal -----------------------------------------------------

    def close_edge(self, fact_id: str, at: str) -> None:
        for edge in self.edges:
            if edge.fact_id == fact_id:
                edge.valid_to = at

    def _visible(self, node: Node, allowed: list[str] | None) -> bool:
        return allowed is None or not node.scopes or bool(set(node.scopes) & set(allowed))

    def _resolve(self, value: str, kind: str) -> tuple[list[Node], str]:
        """Anchor -> nodes as ``resolve_candidates`` does: the value as written,
        then with template parameters normalized, by key before alias."""
        normalized = _PLACEHOLDER.sub("{}", value)
        forms = [value] if normalized == value else [value, normalized]
        nodes = [n for n in self.nodes.values() if not kind or n.kind == kind]
        for form in forms:
            for method, hits in (
                ("natural_key", [n for n in nodes if n.key == form]),
                ("alias", [n for n in nodes if form in n.aliases]),
            ):
                if hits:
                    return hits, method
        return [], ""

    async def typed_context(
        self,
        *,
        namespace: str,
        namespaces: list[str],
        request: dict[str, Any],
        trace_run_id: str | None = None,
    ) -> dict[str, Any]:
        scope: dict[str, Any] = {"namespace": namespace}
        if list(namespaces) != [namespace]:
            scope["namespaces"] = list(dict.fromkeys([namespace, *namespaces]))
        body = {**request, "scope": scope}
        # What HttpContextProvider would put on the wire, checked against the pin.
        self._context_in.validate(body)
        self._request_schema.validate(body)
        self.typed_requests.append(body)
        if self.fail == "typed":
            raise ContextProviderError("boom", retryable=True, status=503)
        graph_ns = next((ns for ns in scope.get("namespaces", [namespace]) if ":ws:" in ns), None)
        allowed = body.get("allowedScopes")
        as_of = body.get("as_of")
        entities: dict[str, dict[str, Any]] = {}
        facts: dict[str, dict[str, Any]] = {}

        def accept(key: str) -> dict[str, Any] | None:
            node = self.nodes.get(key)
            if node is None or graph_ns is None or not self._visible(node, allowed):
                return None
            if key not in entities:
                entities[key] = {
                    "natural_key": key,
                    "kind": node.kind,
                    "namespace": graph_ns,
                    "title": node.title or key,
                    "attributes": dict(node.attributes),
                    "aliases": list(node.aliases),
                    "provenance": None,
                    "source_path": node.source_path,
                    "valid_from": "2026-01-01T00:00:00+00:00",
                    "valid_to": None,
                    "snapshot": {"source": "git:control-plane", "scope": "", "snapshot_id": "s1"},
                    "reached_via": [],
                    "anchor": False,
                    "evidence": "asserted",
                }
            return entities[key]

        anchors_report = []
        anchor_keys: list[str] = []
        for raw in body["anchors"]:
            anchor = raw if isinstance(raw, dict) else {"value": raw}
            value, kind = anchor["value"], anchor.get("kind", "")
            hits, matched_by = self._resolve(value, kind)
            resolved = []
            for node in hits:
                state = accept(node.key)
                if state is not None:
                    state["anchor"] = True
                    anchor_keys.append(node.key)
                    resolved.append({"natural_key": node.key, "kind": node.kind})
            report: dict[str, Any] = {"input": {"kind": kind, "value": value}, "resolved": resolved}
            if resolved:
                report["matchedBy"] = matched_by
            anchors_report.append(report)

        previous = list(dict.fromkeys(anchor_keys))
        for index, step in enumerate(body.get("traverse") or []):
            start = (
                list(dict.fromkeys(anchor_keys))
                if step.get("from", "anchors") == "anchors"
                else previous
            )
            directions = (
                ["out", "in"]
                if step.get("direction", "out") == "both"
                else [step.get("direction", "out")]
            )
            reached: list[str] = []
            frontier, seen = list(start), set(start)
            for _ in range(step.get("depth", 1)):
                following: list[str] = []
                for key in frontier:
                    for edge in self.edges:
                        if edge.relation != step["relation"] or not edge.valid_at(as_of):
                            continue
                        for direction in directions:
                            if direction == "out" and edge.subject == key:
                                other = edge.object
                            elif direction == "in" and edge.object == key:
                                other = edge.subject
                            else:
                                continue
                            if other not in seen and len(reached) >= step.get("limit", 20):
                                continue
                            state = accept(other)
                            if state is None:
                                continue
                            facts[edge.fact_id] = {
                                "fact_id": edge.fact_id,
                                "relation": edge.relation,
                                "subject": edge.subject,
                                "object": edge.object,
                                "valid_from": edge.valid_from,
                                "valid_to": edge.valid_to,
                                "evidence": "asserted",
                                "source_path": "",
                                "snapshot": {
                                    "source": "git:control-plane",
                                    "scope": "",
                                    "snapshot_id": edge.snapshot_id,
                                },
                            }
                            if other not in seen:
                                seen.add(other)
                                reached.append(other)
                                following.append(other)
                                state["reached_via"].append(
                                    {"step": index, "relation": edge.relation, "from": key}
                                )
                frontier = following
            previous = reached

        by_kind: dict[str, list[dict[str, Any]]] = {}
        for entity in entities.values():
            by_kind.setdefault(entity["kind"], []).append(entity)
        sections = [
            {"kind": kind, "items": sorted(items, key=lambda e: e["natural_key"])}
            for kind, items in sorted(by_kind.items())
        ]
        ordered_facts = sorted(facts.values(), key=lambda f: f["fact_id"])
        snapshots = []
        for item in [*entities.values(), *ordered_facts]:
            if item["snapshot"] not in snapshots:
                snapshots.append(item["snapshot"])
        return {
            "as_of": as_of or "",
            "namespaces": scope.get("namespaces", [namespace]),
            "anchors": anchors_report,
            "unresolved": [a["input"] for a in anchors_report if not a["resolved"]],
            "sections": sections,
            "facts": ordered_facts,
            "used": {
                "entities": [{"namespace": graph_ns, "natural_key": k} for k in entities],
                "facts": [f["fact_id"] for f in ordered_facts],
                "snapshots": snapshots,
            },
            "sources": [],
            "trace_id": f"ctx-{uuid.uuid4().hex[:16]}",
        }
