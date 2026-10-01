"""``catalog_key_busy`` is told apart from other refusals (CP-ADR-0074, amendment 2026-09-29)."""

from control_plane.application.commands.catalog_retirements import (
    KEY_BUSY,
    PROCESS,
    is_key_busy,
    key_busy,
)
from control_plane.domain.errors import ConflictError, DependencyUnavailableError


def test_a_busy_key_is_told_apart() -> None:
    exc = key_busy(PROCESS, "sample")
    assert exc.code == KEY_BUSY
    assert exc.http_status == 503
    assert exc.details == {"kind": PROCESS, "key": "sample"}
    assert is_key_busy(exc)


def test_other_dependency_failures_are_not_a_busy_key() -> None:
    assert not is_key_busy(DependencyUnavailableError("the PDP is down"))
    assert not is_key_busy(DependencyUnavailableError("memory is off", code="memory_disabled"))


def test_an_error_of_another_kind_with_the_same_code_is_not_a_busy_key() -> None:
    assert not is_key_busy(ConflictError(KEY_BUSY, "not a dependency failure"))
    assert not is_key_busy(RuntimeError(KEY_BUSY))
