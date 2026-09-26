import uuid

import pytest

from control_plane.application.authorization import AuthContext, require
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError


def make_ctx(*permissions: str) -> AuthContext:
    return AuthContext(
        tenant_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        principal_kind="agent",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(permissions),
    )


def test_admin_implies_everything() -> None:
    ctx = make_ctx("admin")
    for permission in Permission:
        assert ctx.has(permission)


def test_specific_permission() -> None:
    ctx = make_ctx("tasks.read")
    assert ctx.has(Permission.TASKS_READ)
    assert not ctx.has(Permission.TASKS_WRITE)


def test_require_any_of() -> None:
    ctx = make_ctx("claims.manage")
    require(ctx, Permission.TASKS_CLAIM, Permission.CLAIMS_MANAGE)  # no raise
    with pytest.raises(AuthorizationError) as excinfo:
        require(ctx, Permission.TASKS_WRITE)
    assert excinfo.value.http_status == 403
    assert excinfo.value.details["required"] == ["tasks.write"]


def test_redaction() -> None:
    from control_plane.logging import redact

    data = {"authorization": "Bearer cp_x", "nested": {"api_key": "secret", "ok": 1}}
    cleaned = redact(data)
    assert cleaned == {"authorization": "[REDACTED]", "nested": {"api_key": "[REDACTED]", "ok": 1}}
