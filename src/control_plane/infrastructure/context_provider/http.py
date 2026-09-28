"""HTTP reference implementation of the Context Memory provider contract.

Wire contract (the Memory Service's public ``/api/memory/*`` API):

* ``POST /api/memory/observations:batch`` → 207 with per-item results and
  ``accepted``/``duplicates``/``failed`` counters. Write scope is exactly one
  namespace, passed as ``{"scope": {"namespace": ...}}``.
* ``POST /api/brain/documents`` → 201: one document (node and text chunks),
  idempotent by ``natural_key`` (case documents, CP-ADR-0076 §2);
* ``POST /api/memory/context`` → 200 ContextPack (sections, sources,
  token_estimate, budget, trace_id). Read scope likewise, plus
  ``scope.namespaces`` when the read spans the tenant namespace and a
  workspace namespace (CP-ADR-0059).
* ``POST /api/memory/reconcile`` → reconcile one knowledge snapshot into a
  namespace (CP-ADR-0060). The body is the FLAT snapshot document (``pack``,
  ``source``, ``scope`` -- a string of the source --, ``snapshotId``,
  ``observedAt``, ``entities``, ``relations``) plus top-level ``namespace`` and
  ``scopes`` (Memory's ``ReconcileIn``). Answer: counters and ``duplicate``;
  400 for an invalid snapshot, 409 for one older than the one already applied.
* ``POST /api/memory/packages`` → register a domain knowledge pack (the
  manifest as is; 201 created, 200 unchanged, 409 version exists with other
  content); ``PUT /api/memory/namespaces/{ns}/kinds`` with
  ``{"strict", "packages"}`` → enabled packs and strict mode of a namespace
  (404 for an unknown pack reference).
* ``POST /api/memory/context/typed`` → typed traversal of the knowledge graph
  (CP-ADR-0064): ``anchors[{kind?, value}]``, ``traverse[{relation,
  direction, depth, limit, from}]``, ``as_of``, ``allow_semantic`` plus the
  same ``scope`` and visibility fields as ``/context``. Answer: sections of
  entities by kind, ``facts``, ``used{entities, facts, snapshots}``,
  ``anchors``/``unresolved`` and ``trace_id``.
* ``GET /api/memory/namespaces/{ns}/kinds`` → the kind catalog of a namespace
  (``catalog.packages`` — the enabled packs as ``name@version``);
  ``GET /api/memory/packages/{name}?version=`` → one pack version with the
  ``idPatterns`` of its kinds. Pack versions are immutable.
* ``GET /healthz`` → unauthenticated liveness.

Auth: either a single static bearer token or — the platform way (superproject
ADR-0030, memory-service ADR-018) — the Control Plane's own service account,
exchanged at IAM for a short-lived access token of the memory audience by a
``token_provider`` that is asked before every request. Neither credential ever
reaches harness clients — this module runs server-side only (MCP/CLI/SDK talk
to the Control Plane).
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from control_plane.infrastructure.context_provider.base import ContextProviderError, IngestResult

TokenProvider = Callable[[], Awaitable[str]]


class HttpContextProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        ingest_timeout_seconds: float,
        reconcile_timeout_seconds: float | None = None,
        token_provider: TokenProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"User-Agent": "control-plane-context-adapter/0.5"}
        if api_key and token_provider is None:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, transport=transport
        )
        self._token_provider = token_provider
        self._timeout = timeout_seconds
        self._ingest_timeout = ingest_timeout_seconds
        self._reconcile_timeout = reconcile_timeout_seconds or ingest_timeout_seconds

    async def aclose(self) -> None:
        await self._http.aclose()
        closer = getattr(self._token_provider, "aclose", None)
        if closer is not None:
            await closer()

    async def _auth_headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        try:
            token = await self._token_provider()
        except Exception as exc:  # the SDK reports one opaque reason
            # No token means no delivery; IAM outages are transient by nature, so the
            # adapter backs off and retries the same unit instead of skipping it.
            raise ContextProviderError(
                f"context provider credential unavailable: {exc}", retryable=True
            ) from exc
        return {"Authorization": f"Bearer {token}"}

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        request_timeout: float,
        trace_run_id: str | None = None,
    ) -> httpx.Response:
        return await self._send(
            "POST", path, body, request_timeout=request_timeout, trace_run_id=trace_run_id
        )

    async def _send(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        request_timeout: float,
        trace_run_id: str | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        # X-Run-Id ties this call to the caller's trace on the Memory side
        # (ADR-0039). It is correlation only and carries no authority.
        headers = await self._auth_headers()
        if trace_run_id:
            headers["X-Run-Id"] = trace_run_id
        try:
            response = await self._http.request(
                method,
                path,
                json=body,
                params=params,
                timeout=request_timeout,
                headers=headers or None,
            )
        except httpx.HTTPError as exc:
            raise ContextProviderError(
                f"context provider unreachable: {exc}", retryable=True
            ) from exc
        if response.status_code >= 500:
            raise ContextProviderError(
                f"context provider server error {response.status_code}",
                retryable=True,
                status=response.status_code,
            )
        if response.status_code in (401, 403):
            # Misconfiguration: retrying with the same key cannot succeed,
            # but skipping would lose data — treat as retryable so the
            # adapter parks with a visible diagnostic instead of advancing.
            # A 401 may mean the service-account token was revoked or rotated:
            # drop the cached one so the retry exchanges afresh. A 403 is a valid
            # token lacking a right -- a new token would not help, and dropping
            # the shared one on every refusal would only hammer IAM.
            if response.status_code == 401:
                forget = getattr(self._token_provider, "forget", None)
                if forget is not None:
                    forget()
            raise ContextProviderError(
                f"context provider rejected credentials ({response.status_code})",
                retryable=True,
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise ContextProviderError(
                f"context provider rejected the request ({response.status_code}): "
                f"{response.text[:500]}",
                retryable=False,
                status=response.status_code,
            )
        return response

    async def ingest_batch(
        self,
        *,
        namespace: str,
        observations: list[dict[str, Any]],
        trace_run_id: str | None = None,
    ) -> IngestResult:
        response = await self._post(
            "/api/memory/observations:batch",
            {"observations": observations, "scope": {"namespace": namespace}},
            request_timeout=self._ingest_timeout,
            trace_run_id=trace_run_id,
        )
        # A malformed 2xx leaves delivery state UNKNOWN: retry (safe under
        # at-least-once), never advance past it.
        try:
            body = response.json()
            results = body.get("results", [])
            errors = [r for r in results if "error" in r]
            accepted = int(body.get("accepted", 0))
            duplicates = int(body.get("duplicates", 0))
            reported_failed = int(body.get("failed", 0))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContextProviderError(f"malformed batch response: {exc}", retryable=True) from exc
        # Trust our own error scan over the summary counter: an inconsistent
        # body must not let the cursor advance past a lost observation.
        return IngestResult(
            accepted=accepted,
            duplicates=duplicates,
            failed=max(reported_failed, len(errors)),
            errors=errors,
        )

    async def ingest_document(
        self,
        *,
        namespace: str,
        document: dict[str, Any],
        trace_run_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._post(
            "/api/brain/documents",
            {**document, "scope": {"namespace": namespace}},
            request_timeout=self._ingest_timeout,
            trace_run_id=trace_run_id,
        )
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ContextProviderError(
                f"malformed document response: {exc}", retryable=True
            ) from exc
        return body

    async def build_context(
        self,
        *,
        namespace: str,
        request: dict[str, Any],
        trace_run_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        body = dict(request)
        scope: dict[str, Any] = {"namespace": namespace}
        # A single-namespace read keeps the historical wire shape; the list is
        # sent only when there is more than the tenant namespace to read.
        if namespaces and list(namespaces) != [namespace]:
            scope["namespaces"] = list(dict.fromkeys([namespace, *namespaces]))
        body["scope"] = scope
        response = await self._post(
            "/api/memory/context",
            body,
            request_timeout=self._timeout,
            trace_run_id=trace_run_id,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ContextProviderError("malformed ContextPack response", retryable=False) from exc
        if not isinstance(data, dict):
            raise ContextProviderError("malformed ContextPack response", retryable=False)
        # NaN/Infinity survive json.loads but are unserializable downstream
        # (strict JSON) — reject here so /context degrades instead of 500ing.
        try:
            json.dumps(data, allow_nan=False)
        except ValueError as exc:
            raise ContextProviderError(
                "ContextPack contains non-JSON numbers", retryable=False
            ) from exc
        return data

    async def _json_object(self, response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ContextProviderError(f"malformed {what} response", retryable=True) from exc
        if not isinstance(data, dict):
            raise ContextProviderError(f"malformed {what} response", retryable=True)
        try:
            json.dumps(data, allow_nan=False)
        except ValueError as exc:
            raise ContextProviderError(
                f"{what} response contains non-JSON numbers", retryable=True
            ) from exc
        return data

    async def reconcile_snapshot(
        self,
        *,
        namespace: str,
        scopes: list[str],
        snapshot: dict[str, Any],
        trace_run_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._post(
            "/api/memory/reconcile",
            {**snapshot, "namespace": namespace, "scopes": scopes},
            request_timeout=self._reconcile_timeout,
            trace_run_id=trace_run_id,
        )
        return await self._json_object(response, "reconcile")

    async def register_package(
        self, *, package: dict[str, Any], trace_run_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._post(
            "/api/memory/packages",
            package,
            request_timeout=self._reconcile_timeout,
            trace_run_id=trace_run_id,
        )
        return await self._json_object(response, "package")

    async def set_namespace_kinds(
        self,
        *,
        namespace: str,
        packages: list[str],
        strict: bool,
        trace_run_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._send(
            "PUT",
            f"/api/memory/namespaces/{quote(namespace, safe='')}/kinds",
            {"packages": packages, "strict": strict},
            request_timeout=self._reconcile_timeout,
            trace_run_id=trace_run_id,
        )
        return await self._json_object(response, "namespace kinds")

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
        response = await self._post(
            "/api/memory/context/typed",
            {**request, "scope": scope},
            request_timeout=self._timeout,
            trace_run_id=trace_run_id,
        )
        # A malformed pack is not worth a retry within the same request: the
        # caller degrades it like any other provider failure.
        try:
            return await self._json_object(response, "typed context")
        except ContextProviderError as exc:
            raise ContextProviderError(str(exc), retryable=False) from exc

    async def namespace_kinds(
        self, *, namespace: str, trace_run_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._send(
            "GET",
            f"/api/memory/namespaces/{quote(namespace, safe='')}/kinds",
            None,
            request_timeout=self._timeout,
            trace_run_id=trace_run_id,
        )
        return await self._json_object(response, "namespace kinds")

    async def get_package(
        self, *, name: str, version: str = "", trace_run_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._send(
            "GET",
            f"/api/memory/packages/{quote(name, safe='')}",
            None,
            request_timeout=self._timeout,
            trace_run_id=trace_run_id,
            params={"version": version} if version else None,
        )
        return await self._json_object(response, "package")

    async def healthy(self) -> bool:
        try:
            response = await self._http.get("/healthz", timeout=self._timeout)
        except httpx.HTTPError:
            return False
        return response.status_code == 200
