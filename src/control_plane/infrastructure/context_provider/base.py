"""Neutral Context Memory provider boundary.

The Control Plane talks to an external Context Memory Engine over HTTP and
ONLY over HTTP: no shared database, no imported models, no shared
transactions. The provider is optional — with ``CP_CONTEXT_PROVIDER=none``
every coordination path works unchanged and readiness is unaffected.

DTOs on this boundary are plain dicts shaped by the provider's public HTTP
contract (see application/context/mapping.py for observations); duplication
of that contract here is deliberate and is pinned by contract tests against
the real service.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class ContextProviderError(Exception):
    """Provider interaction failed.

    ``retryable`` distinguishes transient transport/5xx conditions (back off
    and retry the same delivery unit) from permanent rejections (4xx
    validation): permanent failures must never be skipped silently — the
    adapter stops at the poison unit and surfaces diagnostics.
    """

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one observation batch delivery."""

    accepted: int = 0
    duplicates: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fully_delivered(self) -> bool:
        return self.failed == 0


class ContextProvider(Protocol):
    """What the Control Plane needs from a Context Memory Engine."""

    async def ingest_batch(
        self,
        *,
        namespace: str,
        observations: list[dict[str, Any]],
        trace_run_id: str | None = None,
    ) -> IngestResult:
        """Deliver observations into one namespace (at-least-once)."""
        ...

    async def build_context(
        self,
        *,
        namespace: str,
        request: dict[str, Any],
        trace_run_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build a bounded ContextPack for the given request.

        ``namespace`` is the primary (tenant) namespace; ``namespaces``, when
        given, is the full read set — the primary first, then the workspace
        namespaces the Control Plane has already authorized.
        """
        ...

    async def healthy(self) -> bool:
        """Cheap liveness probe for diagnostics (never gates readiness)."""
        ...

    async def aclose(self) -> None: ...


class KnowledgeProvider(Protocol):
    """Knowledge snapshots and domain packs (CP-ADR-0060).

    The core proxies these with its own identity after authorizing the caller;
    namespace and visibility scopes are always computed by the core.
    Responses are the Memory Service's JSON objects, passed through as-is.
    """

    async def reconcile_snapshot(
        self,
        *,
        namespace: str,
        scopes: list[str],
        snapshot: dict[str, Any],
        trace_run_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def register_package(
        self, *, package: dict[str, Any], trace_run_id: str | None = None
    ) -> dict[str, Any]: ...

    async def set_namespace_kinds(
        self,
        *,
        namespace: str,
        packages: list[str],
        strict: bool,
        trace_run_id: str | None = None,
    ) -> dict[str, Any]: ...


class GraphProvider(Protocol):
    """Typed traversal of the knowledge graph (CP-ADR-0064, TAI-ADR-0042 p.4-6).

    The task context pack and ``cp_recall`` both go through these calls — one
    client, one wire format. The core reads with its own identity and hands
    Memory the visibility it computed for the caller, as ``/context`` does.
    """

    async def typed_context(
        self,
        *,
        namespace: str,
        namespaces: list[str],
        request: dict[str, Any],
        trace_run_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def namespace_kinds(
        self, *, namespace: str, trace_run_id: str | None = None
    ) -> dict[str, Any]: ...

    async def get_package(
        self, *, name: str, version: str = "", trace_run_id: str | None = None
    ) -> dict[str, Any]: ...
