"""Policy Enforcement Point over IAM and Entitlement (IAM-7).

The Control Plane stays a resource server: it verifies someone else's token but
never issues credentials and never stores licences. The order of the decision is
fixed by the SDK — identity, revocation, entitlement, domain policy — so what is
described here is only the two things the SDK cannot know.

The first is **where permissions come from**. An IAM token carries no Control
Plane permissions, and that is not an omission: the right to create a Task
belongs to the product, not to an identity provider. So an external identity is
mapped onto a local Principal through ``iam_principal_bindings``, and the
permissions are read from there.

The second is **what bounds those permissions**. The scope of the presented
token acts as a ceiling: a binding may allow writes, yet a token issued for
reading only will not write. The intersection narrows, it never widens. A
``control-plane:decide`` token narrows further still, to deciding the one
approval its ``purpose_ref`` names (CP-ADR-0070).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from platform_auth import (
    CachingRevocationDirectory,
    CredentialStatus,
    DecisionRecord,
    EnforcementError,
    EntitlementClient,
    EntitlementPolicy,
    EntitlementUnavailable,
    InvalidToken,
    JwksCache,
    JwksPolicy,
    NullEntitlementClient,
    PolicyEnforcementPoint,
    ServiceCredentials,
    ServiceTokenProvider,
    TokenVerifier,
    TrustedAuthContext,
    VerificationUnavailable,
    VerifierConfig,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.application.authorization import AuthContext
from control_plane.application.events import current_iam_actor
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    DependencyUnavailableError,
    DomainError,
)
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import IamPrincipalBinding, Principal

logger = logging.getLogger("control_plane.authz")

# The Control Plane scope ceiling. These values are declared in the IAM audience
# registry; this service only checks that the presented token carries them.
SCOPE_READ = "control-plane:read"
SCOPE_WRITE = "control-plane:write"
SCOPE_ADMIN = "control-plane:admin"
# A decision taken from a channel (CP-ADR-0070): the token decides the one
# approval named by its ``purpose_ref`` and can do nothing else.
SCOPE_DECIDE = "control-plane:decide"
ANY_SCOPE = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN, SCOPE_DECIDE)

CHANNEL_ACR_PREFIX = "channel:"
_CHANNEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_APPROVAL_PURPOSE = re.compile(r"^approval:(?P<id>[0-9a-fA-F-]{36})$")
# The only requests a decision token may make: approve or reject.
_DECISION_ACTION = re.compile(r"^POST /api/v1/approvals/(?P<id>[^/:]+):(?:approve|reject)$")

# IAM principal kind -> Control Plane principal kind. IAM separates
# service_account from workload, the Control Plane does not: for its domain
# model both are equally non-interactive participants.
PRINCIPAL_KINDS = {
    "human": "human",
    "agent": "agent",
    "service_account": "service",
    "workload": "service",
}


def narrow_permissions(
    granted: list[str] | frozenset[str], scopes: frozenset[str]
) -> frozenset[str]:
    """Intersect local permissions with the ceiling of the presented token.

    The rule is deliberately simple, which is what makes it checkable: ``admin``
    needs the admin scope, a read permission needs the read scope, everything
    else needs the write scope. An admin scope adds nothing to the binding — it
    only refrains from narrowing it.
    """
    permissions = frozenset(granted)
    if SCOPE_DECIDE in scopes:
        # The decision scope overrides every other scope on the same token: a
        # token for one decision stays one, whatever else it was issued with.
        decide = Permission.APPROVALS_DECIDE.value
        if decide in permissions or Permission.ADMIN.value in permissions:
            return frozenset({decide})
        return frozenset()
    if SCOPE_ADMIN in scopes:
        return permissions

    allowed: set[str] = set()
    for permission in permissions:
        if permission == Permission.ADMIN.value:
            # Full admin is only granted under the admin scope; otherwise a
            # read-only token would raise the binding owner to administrator.
            continue
        if permission.endswith(".read"):
            if SCOPE_READ in scopes:
                allowed.add(permission)
        elif SCOPE_WRITE in scopes:
            allowed.add(permission)
    return frozenset(allowed)


def channel_of(acr: str) -> str | None:
    """The channel of entry named by ``acr=channel:<name>``, if any."""
    if not acr.startswith(CHANNEL_ACR_PREFIX):
        return None
    name = acr[len(CHANNEL_ACR_PREFIX) :]
    return name if _CHANNEL.match(name) else None


def decision_purpose(claims: Mapping[str, Any], action: str) -> str:
    """Check a decision token against the request it is presented with.

    The token names one approval (``purpose_ref=approval:<id>``) and is good
    for approving or rejecting exactly that one. Everything else — another
    approval, a read, any other write, the event stream — is refused here,
    before any data is loaded, so a refusal tells nothing about what exists.
    """
    raw = claims.get("purpose_ref")
    match = _APPROVAL_PURPOSE.match(raw) if isinstance(raw, str) else None
    if match is None:
        raise AuthorizationError(
            "A decision token must name the approval it decides",
            code="purpose_ref_required",
        )
    approval_id = uuid.UUID(match["id"])
    requested = _DECISION_ACTION.match(action)
    try:
        target = uuid.UUID(requested["id"]) if requested is not None else None
    except ValueError:
        target = None
    if target != approval_id:
        raise AuthorizationError(
            "A decision token is good only for deciding its own approval",
            code="outside_purpose",
        )
    return f"approval:{approval_id}"


def feature_for_path(path: str, default: str) -> str:
    """Entitlement feature derived from the request path.

    What is licensed is an API area rather than a single endpoint:
    ``/api/v1/tasks/...`` is the ``tasks`` feature. Anything that does not parse
    falls back to the default feature instead of skipping the check.
    """
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) >= 3 and parts[0] == "api":
        return parts[2]
    return default


def looks_like_iam_token(token: str) -> bool:
    """Tell a JWT apart from the legacy ``cp_<prefix>_<secret>`` key.

    This is a decision by shape, not an attempt to verify: identifying a
    credential by trying authentication methods in turn would leak, through the
    response code, which one matched.
    """
    parts = token.split(".")
    return len(parts) == 3 and all(parts) and not token.startswith("cp_")


def to_domain_error(error: EnforcementError) -> DomainError:
    """Map an SDK denial onto the single Control Plane error envelope.

    Codes are carried over verbatim: they are the same across platform services,
    so a client does not have to learn a different dictionary per service.
    """
    if isinstance(error, InvalidToken):
        return AuthenticationError()
    if isinstance(error, VerificationUnavailable | EntitlementUnavailable):
        return DependencyUnavailableError(code=error.code, details=error.details)
    return AuthorizationError(code=error.code, details=error.details)


@dataclass(frozen=True)
class BindingSnapshot:
    """A resolved projection: who this is locally and what they may do here."""

    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    principal_kind: str
    binding_id: uuid.UUID
    permissions: frozenset[str]
    iam_principal_id: uuid.UUID | None = None


class BindingDirectory:
    """Source of the local revocation policy and of federated permissions.

    It lives as long as the application and opens its own short transaction,
    exactly as the API key check does. Caching and the hard staleness bound come
    from the SDK so that the degradation policy is one and the same across
    services.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: float,
        stale_after_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._stale_after = stale_after_seconds
        self._snapshots: dict[str, tuple[datetime, BindingSnapshot]] = {}
        # Which credentials were answered from which binding: the revocation
        # cache is keyed by credential, a binding change is keyed by identity,
        # and this is the bridge between the two for ``invalidate``.
        self._credentials: dict[str, set[tuple[str, str]]] = {}
        self._directory = CachingRevocationDirectory(
            self._load,
            ttl_seconds=ttl_seconds,
            stale_after_seconds=stale_after_seconds,
            clock=clock,
        )

    @staticmethod
    def _key(ctx: TrustedAuthContext) -> str:
        return f"{ctx.issuer}|{ctx.principal_id}"

    async def check(self, ctx: TrustedAuthContext) -> CredentialStatus:
        return await self._directory.check(ctx)

    def snapshot(self, ctx: TrustedAuthContext) -> BindingSnapshot | None:
        entry = self._snapshots.get(self._key(ctx))
        return entry[1] if entry is not None else None

    def forget(self, ctx: TrustedAuthContext) -> None:
        self._snapshots.pop(self._key(ctx), None)
        self._directory.forget(str(ctx.tenant_id), ctx.credential_id)

    def invalidate(self, issuer: str, iam_principal_id: uuid.UUID) -> None:
        """Forget everything cached for one identity after its binding changed.

        A binding created, repointed or revoked through the API takes effect
        on the next request of this process instead of after the cache TTL —
        including a negative answer cached before the binding existed.
        """
        key = f"{issuer}|{iam_principal_id}"
        self._snapshots.pop(key, None)
        for tenant_id, credential_id in self._credentials.pop(key, ()):
            self._directory.forget(tenant_id, credential_id)

    async def _load(self, ctx: TrustedAuthContext) -> CredentialStatus:
        self._credentials.setdefault(self._key(ctx), set()).add(
            (str(ctx.tenant_id), ctx.credential_id)
        )
        async with transaction(self._session_factory) as session:
            row = (
                await session.execute(
                    select(IamPrincipalBinding, Principal)
                    .join(Principal, Principal.id == IamPrincipalBinding.principal_id)
                    .where(
                        IamPrincipalBinding.issuer == ctx.issuer,
                        IamPrincipalBinding.iam_principal_id == ctx.principal_id,
                    )
                )
            ).first()
            if row is not None:
                # The last-used stamp is written here and only here: the binding
                # is read no more often than once per cache TTL, so this write
                # never lands on the hot path of every request.
                await session.execute(
                    update(IamPrincipalBinding)
                    .where(IamPrincipalBinding.id == row[0].id)
                    .values(last_used_at=self._clock())
                )

        if row is None:
            # An unknown identity answers exactly like a revoked one; otherwise
            # the difference in responses exposes someone else's directory.
            return CredentialStatus.revoked("binding_not_found")

        binding, principal = row
        if binding.iam_tenant_id != ctx.tenant_id:
            return CredentialStatus.revoked("tenant_mismatch")
        if binding.status != "active" or binding.revoked_at is not None:
            return CredentialStatus.revoked("binding_disabled")
        if principal.status != "active":
            return CredentialStatus.revoked("principal_not_active")

        self._prune()
        self._snapshots[self._key(ctx)] = (
            self._clock(),
            BindingSnapshot(
                tenant_id=binding.tenant_id,
                principal_id=binding.principal_id,
                principal_kind=principal.kind,
                binding_id=binding.id,
                permissions=frozenset(binding.permissions),
                iam_principal_id=binding.iam_principal_id,
            ),
        )
        return CredentialStatus.allowed()

    def _prune(self) -> None:
        now = self._clock()
        horizon = self._stale_after * 2
        stale = [
            key
            for key, (stored_at, _) in self._snapshots.items()
            if (now - stored_at).total_seconds() > horizon
        ]
        for key in stale:
            self._snapshots.pop(key, None)
            self._credentials.pop(key, None)


class LoggingAuditSink:
    """Authorization decisions go to the service's shared JSON log.

    A dedicated table is not needed here: the record takes no part in domain
    transactions, and the correlation id is what ties it to the request.
    """

    def record(self, decision: DecisionRecord) -> None:
        payload = decision.as_dict()
        if decision.outcome == "allowed":
            logger.info("authz decision", extra={"decision": payload})
        else:
            logger.warning("authz denied", extra={"decision": payload})


@dataclass
class IamEnforcement:
    """The assembled PEP together with what it owns."""

    pep: PolicyEnforcementPoint
    bindings: BindingDirectory
    default_feature: str
    entitlement_enabled: bool
    closables: list[object]

    async def aclose(self) -> None:
        for closable in self.closables:
            aclose = getattr(closable, "aclose", None)
            if aclose is not None:
                await aclose()


def build_iam_enforcement(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> IamEnforcement | None:
    """Assemble the PEP when IAM is enabled.

    An incomplete configuration fails startup rather than silently falling back
    to the previous mode: a service that has "almost" moved to IAM is worse than
    either end state.
    """
    if not settings.iam_enabled:
        return None
    if not settings.iam_issuer or not settings.iam_jwks_url:
        raise ValueError("CP_IAM_ENABLED requires CP_IAM_ISSUER and CP_IAM_JWKS_URL")

    closables: list[object] = []
    keys = JwksCache(
        settings.iam_jwks_url,
        policy=JwksPolicy(
            refresh_after_seconds=settings.iam_jwks_refresh_after_seconds,
            stale_after_seconds=settings.iam_jwks_stale_after_seconds,
            min_refresh_interval_seconds=settings.iam_jwks_min_refresh_interval_seconds,
            request_timeout_seconds=settings.iam_request_timeout_seconds,
        ),
    )
    closables.append(keys)

    verifier = TokenVerifier(
        keys,
        VerifierConfig(
            issuer=settings.iam_issuer,
            audience=settings.iam_audience,
            leeway_seconds=settings.iam_leeway_seconds,
        ),
    )

    entitlement: EntitlementClient | NullEntitlementClient
    if settings.entitlement_enabled:
        if not settings.iam_client_id or not settings.iam_client_secret:
            raise ValueError(
                "CP_ENTITLEMENT_ENABLED requires CP_IAM_CLIENT_ID and CP_IAM_CLIENT_SECRET"
            )
        token_provider = ServiceTokenProvider(
            settings.iam_base_url,
            ServiceCredentials(
                client_id=settings.iam_client_id,
                client_secret=settings.iam_client_secret,
                audience=settings.entitlement_audience,
                scopes=tuple(settings.iam_client_scopes),
            ),
            request_timeout_seconds=settings.iam_request_timeout_seconds,
        )
        closables.append(token_provider)
        entitlement = EntitlementClient(
            settings.entitlement_base_url,
            token_provider,
            product=settings.entitlement_product,
            policy=EntitlementPolicy(
                cache_ttl_seconds=settings.entitlement_cache_ttl_seconds,
                degraded_max_age_seconds=settings.entitlement_degraded_max_age_seconds,
                request_timeout_seconds=settings.entitlement_timeout_seconds,
            ),
        )
        closables.append(entitlement)
    else:
        entitlement = NullEntitlementClient(settings.entitlement_product)

    bindings = BindingDirectory(
        session_factory,
        ttl_seconds=settings.iam_binding_cache_ttl_seconds,
        stale_after_seconds=settings.iam_binding_stale_after_seconds,
    )

    return IamEnforcement(
        pep=PolicyEnforcementPoint(
            verifier,
            entitlement=entitlement,
            revocation=bindings,
            audit=LoggingAuditSink(),
        ),
        bindings=bindings,
        default_feature=settings.entitlement_default_feature,
        entitlement_enabled=settings.entitlement_enabled,
        closables=closables,
    )


async def authenticate_with_iam(
    enforcement: IamEnforcement,
    token: str,
    *,
    action: str,
    path: str,
    request_id: str,
    correlation_id: str = "",
    trace_run_id: str = "",
) -> AuthContext:
    """Run the PEP and turn a verified identity into a local AuthContext.

    Domain policy is not applied at this step: the concrete permission is still
    checked by the endpoint or the command through ``require(...)``. What is
    settled here is only what this identity is allowed to ask of the Control
    Plane at all.
    """
    allowed = await enforcement.pep.enforce(
        token,
        action=action,
        feature=feature_for_path(path, enforcement.default_feature),
        required_scopes=ANY_SCOPE,
        correlation_id=correlation_id or request_id,
    )
    ctx = allowed.context

    snapshot = enforcement.bindings.snapshot(ctx)
    if snapshot is None:  # pragma: no cover - guards against a cache skew
        raise LookupError("binding_snapshot_missing")

    scopes = ctx.effective_scopes()
    purpose_ref = decision_purpose(ctx.claims, action) if SCOPE_DECIDE in scopes else None

    auth_ctx = AuthContext(
        tenant_id=snapshot.tenant_id,
        principal_id=snapshot.principal_id,
        principal_kind=PRINCIPAL_KINDS.get(ctx.principal_type, snapshot.principal_kind),
        # For a federated identity the carrier of permissions is the binding,
        # not an API key.
        api_key_id=snapshot.binding_id,
        permissions=narrow_permissions(snapshot.permissions, scopes),
        request_id=request_id,
        correlation_id=correlation_id or request_id,
        trace_run_id=trace_run_id,
        iam_principal_id=snapshot.iam_principal_id or ctx.principal_id,
        channel=channel_of(ctx.acr),
        purpose_ref=purpose_ref,
    )
    # The journal records the IAM identity next to the local actor (CP-ADR-0055):
    # the PDP projection needs the subject the token names, not principals.id.
    current_iam_actor.set(auth_ctx.iam_principal_id)
    return auth_ctx
