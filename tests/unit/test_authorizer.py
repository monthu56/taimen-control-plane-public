"""Authorizer modes (CP-ADR-0055): local, shadow, policy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from platform_auth import (
    AuthorizationUnavailable,
    ObjectPage,
    PolicyDecision,
    ResourceRef,
)

from control_plane import observability
from control_plane.application.authorization import (
    AuthContext,
    Authorizer,
    authorize,
    configure_authorizer,
    get_authorizer,
    visible_objects,
)
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError, DependencyUnavailableError


@dataclass
class FakePolicy:
    allowed: bool = True
    fail: Exception | None = None
    objects: list[str] = field(default_factory=list)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def check(
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):
        self.calls.append((action, resource.key, on_behalf_of))
        if self.fail is not None:
            raise self.fail
        return PolicyDecision(
            allowed=self.allowed,
            reason_code="allowed" if self.allowed else "denied_no_binding",
            decision_id=str(uuid.uuid4()),
            policy_version="1",
            model_version="1",
            source="online",
            consistency_token=None,
            evaluated_at=datetime.now(UTC),
            action=action,
            resource=resource.key,
        )

    async def list_objects(
        self,
        ctx,
        action,
        resource_type,
        *,
        on_behalf_of=None,
        consistency="default",
        cursor=None,
        limit=1000,
    ):
        self.calls.append((action, resource_type, on_behalf_of))
        if self.fail is not None:
            raise self.fail
        return ObjectPage(objects=list(self.objects), cursor=None, model_version="1")


def make_ctx(*permissions: str, iam: bool = True) -> AuthContext:
    return AuthContext(
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(permissions),
        iam_principal_id=uuid.uuid4() if iam else None,
    )


@pytest.fixture(autouse=True)
def _reset_authorizer():
    observability.reset()
    yield
    configure_authorizer(Authorizer(None, "local"))


async def test_local_mode_is_require() -> None:
    configure_authorizer(Authorizer(None, "local"))
    await authorize(make_ctx("tasks.read"), Permission.TASKS_READ)
    with pytest.raises(AuthorizationError):
        await authorize(make_ctx("tasks.read"), Permission.TASKS_WRITE)
    assert await visible_objects(make_ctx("tasks.read"), Permission.TASKS_READ, "workspace") is None


def test_mode_requires_client() -> None:
    with pytest.raises(ValueError):
        Authorizer(None, "shadow")
    with pytest.raises(ValueError):
        Authorizer(FakePolicy(), "weird")  # type: ignore[arg-type]


async def test_shadow_local_decides_and_counts_divergence() -> None:
    policy = FakePolicy(allowed=False)
    configure_authorizer(Authorizer(policy, "shadow"))
    ctx = make_ctx("tasks.read")
    await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", "t1"))  # no raise
    assert observability.counters()["authz_shadow_divergence_total"] == 1
    assert policy.calls == [("tasks.read", "task:t1", str(ctx.iam_principal_id))]
    # local deny stays a deny even if the PDP allows
    policy.allowed = True
    with pytest.raises(AuthorizationError):
        await authorize(ctx, Permission.TASKS_WRITE)
    assert observability.counters()["authz_shadow_divergence_total"] == 2
    assert policy.calls[-1][1].startswith("tenant:")


async def test_shadow_survives_pdp_outage_and_legacy_keys() -> None:
    policy = FakePolicy(fail=AuthorizationUnavailable("policy_service_unavailable"))
    configure_authorizer(Authorizer(policy, "shadow"))
    await authorize(make_ctx("tasks.read"), Permission.TASKS_READ)
    assert observability.counters()["authz_shadow_unavailable_total"] == 1
    await authorize(make_ctx("tasks.read", iam=False), Permission.TASKS_READ)
    assert observability.counters()["authz_shadow_skipped_total"] == 1


async def test_policy_mode_pdp_decides() -> None:
    policy = FakePolicy(allowed=True)
    configure_authorizer(Authorizer(policy, "policy"))
    # no flat permissions at all: the PDP decides
    ctx = make_ctx()
    await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("workspace", "w1"))
    policy.allowed = False
    with pytest.raises(AuthorizationError) as excinfo:
        await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("workspace", "w1"))
    assert excinfo.value.details["reasonCode"] == "denied_no_binding"
    assert excinfo.value.details["resource"] == "workspace:w1"


async def test_policy_mode_any_of_and_outage() -> None:
    policy = FakePolicy(allowed=False)
    configure_authorizer(Authorizer(policy, "policy"))
    ctx = make_ctx()
    with pytest.raises(AuthorizationError):
        await authorize(ctx, Permission.TASKS_CLAIM, Permission.CLAIMS_MANAGE)
    assert [c[0] for c in policy.calls] == ["tasks.claim", "claims.manage"]
    policy.fail = AuthorizationUnavailable("policy_service_unavailable")
    with pytest.raises(DependencyUnavailableError):
        await authorize(ctx, Permission.TASKS_READ)


async def test_policy_mode_legacy_key_falls_back_to_require() -> None:
    policy = FakePolicy(allowed=False)
    configure_authorizer(Authorizer(policy, "policy"))
    await authorize(make_ctx("tasks.read", iam=False), Permission.TASKS_READ)
    assert policy.calls == []


async def test_purpose_bound_credential_keeps_its_ceiling_in_every_mode() -> None:
    """CP-ADR-0070: the PDP knows the person, not the narrowed channel token."""
    policy = FakePolicy(allowed=True)
    ctx = replace(make_ctx("approvals.decide"), purpose_ref="approval:a1", channel="telegram")
    for mode in ("shadow", "policy"):
        configure_authorizer(Authorizer(policy, mode))
        with pytest.raises(AuthorizationError):
            await authorize(ctx, Permission.TASKS_WRITE, resource=ResourceRef("task", "t1"))
        await authorize(ctx, Permission.APPROVALS_DECIDE, resource=ResourceRef("approval", "a1"))


async def test_visible_objects_only_in_policy_mode() -> None:
    policy = FakePolicy(objects=["w1", "w2"])
    configure_authorizer(Authorizer(policy, "shadow"))
    assert await visible_objects(make_ctx(), Permission.TASKS_READ, "workspace") is None
    configure_authorizer(Authorizer(policy, "policy"))
    assert await visible_objects(make_ctx(), Permission.TASKS_READ, "workspace") == {"w1", "w2"}
    assert get_authorizer().mode == "policy"


async def test_visible_objects_accepts_foreign_catalog_action() -> None:
    policy = FakePolicy(objects=["ws-1", "tenant-t"])
    configure_authorizer(Authorizer(policy, "policy"))
    assert await visible_objects(make_ctx(), "memory.read", "memory_namespace") == {
        "ws-1",
        "tenant-t",
    }
    assert policy.calls[-1][0] == "memory.read"


async def test_memory_visibility_maps_policy_objects_to_namespaces() -> None:
    from control_plane.application.queries.context import memory_visibility
    from control_plane.config import Settings

    policy = FakePolicy(objects=["ws-w1", "principal-p1", "tenant-t1", "bogus"])
    configure_authorizer(Authorizer(policy, "policy"))
    ctx = make_ctx()
    settings = Settings(database_url="postgresql+psycopg://x/y", context_namespace_prefix="tenant:")
    visible = await memory_visibility(ctx, settings)
    assert visible is not None
    names, scopes = visible
    tenant = str(ctx.tenant_id)
    assert names == [
        f"tenant:{tenant}:principal:p1",
        "tenant:t1",
        f"tenant:{tenant}:ws:w1",
    ]
    assert (
        f"principal:{ctx.principal_id}" in scopes and f"principal:{ctx.iam_principal_id}" in scopes
    )
    configure_authorizer(Authorizer(policy, "shadow"))
    assert await memory_visibility(ctx, settings) is None


async def test_inside_a_package_test_the_pdp_decides_only_the_callers_reads() -> None:
    """CP-ADR-0074 Z7: a read in the caller's name of what the test did not write."""
    from control_plane import sandbox

    policy = FakePolicy(allowed=False, objects=["w1"])
    configure_authorizer(Authorizer(policy, "policy"))
    caller = make_ctx("admin")
    other = make_ctx("admin")
    trial = sandbox.Trial(
        clock=datetime(2026, 1, 5, tzinfo=UTC), policy_subjects=frozenset({caller.policy_subject})
    )
    trial.written.add("t-new")
    with sandbox.trial(trial):
        with pytest.raises(AuthorizationError):
            await authorize(caller, Permission.TASKS_READ, resource=ResourceRef("task", "t-old"))
        assert await visible_objects(caller, Permission.TASKS_READ, "workspace") == {"w1"}
        # The rest is the local check: rows of the test, writes, principals of the test.
        await authorize(caller, Permission.TASKS_READ, resource=ResourceRef("task", "t-new"))
        await authorize(caller, Permission.TASKS_WRITE, resource=ResourceRef("task", "t-old"))
        await authorize(other, Permission.TASKS_READ, resource=ResourceRef("task", "t-old"))
        assert await visible_objects(other, Permission.TASKS_READ, "workspace") is None
        # Anything else that would reach the PDP is still refused.
        with pytest.raises(sandbox.SandboxOutgoingCall):
            sandbox.refuse_outgoing("policy")
    assert policy.calls == [
        ("tasks.read", "task:t-old", caller.policy_subject),
        ("tasks.read", "workspace", caller.policy_subject),
    ]
    assert trial.outgoing == ["policy"]
