"""Assembly of the external PDP client (TAI-ADR-0025, CP-ADR-0055).

The Control Plane presents its own service identity to policy-service and
names the end principal ``on_behalf_of``; the scope for that is
``policy:check-on-behalf`` on the service account. The mode decides how much
the PDP is trusted: ``local`` builds no client at all, ``shadow`` and
``policy`` need a complete IAM client configuration — a half-configured PDP
fails startup rather than silently staying local.
"""

from __future__ import annotations

from platform_auth import (
    AuthorizationClient,
    AuthorizationPolicy,
    ServiceCredentials,
    ServiceTokenProvider,
)

from control_plane.application.authorization import AUTHZ_MODES, Authorizer
from control_plane.config import Settings


def build_authorizer(settings: Settings) -> tuple[Authorizer, list[object]]:
    mode = settings.authz_mode
    if mode not in AUTHZ_MODES:
        raise ValueError(f"CP_AUTHZ_MODE must be one of {AUTHZ_MODES}, got {mode!r}")
    if mode == "local":
        return Authorizer(None, "local"), []
    if not settings.iam_client_id or not settings.iam_client_secret:
        raise ValueError(f"CP_AUTHZ_MODE={mode} requires CP_IAM_CLIENT_ID and CP_IAM_CLIENT_SECRET")
    token_provider = ServiceTokenProvider(
        settings.iam_base_url,
        ServiceCredentials(
            client_id=settings.iam_client_id,
            client_secret=settings.iam_client_secret,
            audience=settings.policy_audience,
            scopes=tuple(settings.policy_scopes),
        ),
        request_timeout_seconds=settings.iam_request_timeout_seconds,
    )
    client = AuthorizationClient(
        settings.policy_base_url,
        token_provider,
        policy=AuthorizationPolicy(
            cache_ttl_seconds=settings.policy_cache_ttl_seconds,
            request_timeout_seconds=settings.policy_timeout_seconds,
        ),
    )
    return Authorizer(client, mode), [token_provider, client]
