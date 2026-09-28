"""A Memory Service double that projects assertions the way memory-service does.

The rules are memory-service's own (``observations/ingest.py``,
``graph/facts.py``, MEM-ADR-016): an observation is deduplicated by its source
identity; an entity assertion upserts the node (type, title, properties); a
fact assertion is an edge identified by ``(subject, predicate, object,
valid_from)`` with ``valid_from``/``valid_to`` normalized to UTC seconds —
asserting it again sets its ``valid_to``, so a closing assertion closes the
same edge; a missing end becomes a placeholder node typed by the key's
prefix. Documents are nodes with chunks, idempotent by natural key.
"""

import copy
import datetime as dt
from typing import Any

from control_plane.infrastructure.context_provider.base import IngestResult

Fact = tuple[str, str, str, str]  # (subject, predicate, object, valid_from)


def normalize_ts(value: str | None) -> str | None:
    """memory-service ``graph.facts.normalize_ts``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProjectionMemory:
    def __init__(self) -> None:
        self.observations: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.facts: dict[Fact, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.namespaces: set[str] = set()

    # --- the provider surface the adapter uses ------------------------------------

    async def ingest_batch(
        self,
        *,
        namespace: str,
        observations: list[dict[str, Any]],
        trace_run_id: str | None = None,
    ) -> IngestResult:
        self.namespaces.add(namespace)
        accepted = duplicates = 0
        for observation in observations:
            if self.apply(observation):
                accepted += 1
            else:
                duplicates += 1
        return IngestResult(accepted=accepted, duplicates=duplicates)

    async def ingest_document(
        self,
        *,
        namespace: str,
        document: dict[str, Any],
        trace_run_id: str | None = None,
    ) -> dict[str, Any]:
        self.namespaces.add(namespace)
        key = document["natural_key"]
        self.documents[key] = copy.deepcopy(document)
        self.nodes[key] = {
            "type": document.get("type") or "document",
            "title": document["title"],
            "properties": dict(document.get("properties") or {}),
        }
        return {"natural_key": key, "chunks": len(document.get("chunks") or [])}

    async def aclose(self) -> None:
        return None

    # --- projection --------------------------------------------------------------

    def apply(self, observation: dict[str, Any]) -> bool:
        identity = observation["source"]["external_id"]
        if identity in self.observations:
            return False
        self.observations[identity] = copy.deepcopy(observation)
        for assertion in observation.get("assertions") or []:
            if assertion["assert"] == "entity":
                entity = assertion["entity"]
                self.nodes[entity["key"]] = {
                    "type": entity["type"],
                    "title": entity.get("title") or entity["key"],
                    "properties": copy.deepcopy(entity.get("properties") or {}),
                }
            elif assertion["assert"] == "fact":
                fact = assertion["fact"]
                for end in (fact["subject"], fact["object"]):
                    self.nodes.setdefault(
                        end, {"type": end.split(":", 1)[0], "title": end, "properties": {}}
                    )
                since = normalize_ts(fact.get("valid_from", "")) or ""
                key = (fact["subject"], fact["predicate"], fact["object"], since)
                self.facts[key] = {"valid_to": normalize_ts(fact.get("valid_to"))}
        return True

    # --- reading -------------------------------------------------------------------

    def graph(self) -> dict[str, Any]:
        return copy.deepcopy({"nodes": self.nodes, "facts": self.facts})

    def edges(
        self, subject: str | None = None, predicate: str | None = None, *, open_only: bool = True
    ) -> list[Fact]:
        return sorted(
            key
            for key, value in self.facts.items()
            if (subject is None or key[0] == subject)
            and (predicate is None or key[1] == predicate)
            and (not open_only or value["valid_to"] is None)
        )
