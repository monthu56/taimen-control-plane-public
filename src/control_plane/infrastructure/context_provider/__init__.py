"""Context Memory provider boundary (HTTP-only, optional)."""

from platform_auth import ServiceCredentials, ServiceTokenProvider

from control_plane.config import Settings
from control_plane.infrastructure.context_provider.base import (
    ContextProvider,
    ContextProviderError,
    GraphProvider,
    IngestResult,
    KnowledgeProvider,
)
from control_plane.infrastructure.context_provider.http import HttpContextProvider


def resolve_context_auth(settings: Settings) -> str:
    """Which credential the HTTP provider presents: ``iam`` or ``api_key``.

    ``auto`` prefers the service account — a per-service, short-lived, audited
    identity — and falls back to the static key only while no account exists
    (the first boot, before ``bootstrap.py`` has issued one).
    """
    mode = settings.context_auth
    has_account = bool(settings.iam_client_id and settings.iam_client_secret)
    if mode == "auto":
        return "iam" if has_account else "api_key"
    if mode == "iam":
        if not has_account:
            raise ValueError(
                "CP_CONTEXT_AUTH=iam requires CP_IAM_CLIENT_ID and CP_IAM_CLIENT_SECRET"
            )
        return "iam"
    if mode == "api_key":
        return "api_key"
    raise ValueError(f"unknown CP_CONTEXT_AUTH: {mode!r}")


def build_context_provider(settings: Settings) -> ContextProvider | None:
    """None when the provider is disabled — callers must handle absence."""
    if settings.context_provider == "none":
        return None
    if settings.context_provider == "http":
        token_provider: ServiceTokenProvider | None = None
        api_key = settings.context_api_key
        if resolve_context_auth(settings) == "iam":
            api_key = None
            token_provider = ServiceTokenProvider(
                settings.iam_base_url,
                ServiceCredentials(
                    client_id=settings.iam_client_id,
                    client_secret=settings.iam_client_secret or "",
                    audience=settings.context_iam_audience,
                    # In policy mode the core reads memory on behalf of the principal
                    # and hands Memory the visibility it computed (MEM-ADR-019).
                    scopes=tuple(settings.context_iam_scopes)
                    + (("memory:on-behalf",) if settings.authz_mode == "policy" else ()),
                ),
                request_timeout_seconds=settings.iam_request_timeout_seconds,
            )
        return HttpContextProvider(
            base_url=settings.context_base_url,
            api_key=api_key,
            token_provider=token_provider,
            timeout_seconds=settings.context_timeout_seconds,
            ingest_timeout_seconds=settings.context_ingest_timeout_seconds,
            reconcile_timeout_seconds=settings.context_reconcile_timeout_seconds,
        )
    raise ValueError(f"unknown CP_CONTEXT_PROVIDER: {settings.context_provider!r}")


def tenant_namespace(settings: Settings, tenant_id: object) -> str:
    """Stable tenant → namespace mapping (identity, never display names)."""
    return f"{settings.context_namespace_prefix}{tenant_id}"


def workspace_namespace(settings: Settings, tenant_id: object, root_workspace_id: object) -> str:
    """Memory namespace of a workspace tree, keyed by its root.

    TAI-ADR-0031 p.4, CP-ADR-0059 (context reads), CP-ADR-0060 (knowledge writes).

    Same shape as the ``ws-<id>`` memory_namespace objects of policy mode
    (``queries/context.memory_visibility``)."""
    return f"{tenant_namespace(settings, tenant_id)}:ws:{root_workspace_id}"


__all__ = [
    "ContextProvider",
    "ContextProviderError",
    "GraphProvider",
    "HttpContextProvider",
    "IngestResult",
    "KnowledgeProvider",
    "build_context_provider",
    "resolve_context_auth",
    "tenant_namespace",
    "workspace_namespace",
]
