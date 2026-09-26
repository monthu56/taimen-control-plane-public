"""Typed Control Plane client errors.

Domain semantics surface as distinct exception types — a harness reacts to
``StaleClaimError`` structurally, not by parsing message strings. Every error
carries the machine-readable ``code`` from the server envelope.
"""

from typing import Any


class ControlPlaneError(Exception):
    """Base error: transport reached the server and it answered with an error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class TransportError(ControlPlaneError):
    """The request may or may not have been executed (network failure).

    Safe to retry only with the same Idempotency-Key — the client does that
    automatically for idempotent commands.
    """

    def __init__(self, message: str) -> None:
        super().__init__("transport_error", message, status=0)


class AuthenticationError(ControlPlaneError):
    pass


class PermissionDeniedError(ControlPlaneError):
    pass


class NotFoundError(ControlPlaneError):
    pass


class ValidationError(ControlPlaneError):
    pass


class ConflictError(ControlPlaneError):
    pass


class StaleClaimError(ConflictError):
    """Fencing rejected the operation: this process no longer owns the task."""


class ClaimConflictError(ConflictError):
    """The task is already claimed by a live owner."""


class TaskNotReadyError(ConflictError):
    """Blocking dependencies are not completed."""


class ApprovalRequiredError(ConflictError):
    """A pending gate approval holds the task."""


class NotEligibleError(PermissionDeniedError):
    """The principal does not satisfy the task's organizational requirements."""


class SessionExpiredError(ConflictError):
    pass


class VersionConflictError(ConflictError):
    """Optimistic concurrency (If-Match) mismatch."""


class IdempotencyConflictError(ConflictError):
    pass


class BudgetExceededError(ConflictError):
    """The run exhausted its action/duration budget."""


class SkillUnavailableError(ConflictError):
    pass


class RunNotActiveError(ConflictError):
    pass


class CancelledError(ConflictError):
    pass


_CODE_MAP: dict[str, type[ControlPlaneError]] = {
    "stale_claim": StaleClaimError,
    "task_already_claimed": ClaimConflictError,
    "task_claimed": ClaimConflictError,
    "claim_not_expired": ClaimConflictError,
    "claim_conflict": ClaimConflictError,
    "task_not_ready": TaskNotReadyError,
    "approval_required": ApprovalRequiredError,
    "not_eligible": NotEligibleError,
    "session_expired": SessionExpiredError,
    "session_not_active": SessionExpiredError,
    "version_conflict": VersionConflictError,
    "idempotency_key_reused": IdempotencyConflictError,
    "idempotency_in_flight": IdempotencyConflictError,
    "budget_exceeded": BudgetExceededError,
    "skill_unavailable": SkillUnavailableError,
    "run_not_active": RunNotActiveError,
    "task_cancelled": CancelledError,
    "unsupported_protocol_version": ValidationError,
    "invalid_credentials": AuthenticationError,
    "permission_denied": PermissionDeniedError,
    "not_found": NotFoundError,
}

_STATUS_MAP: dict[int, type[ControlPlaneError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    428: ValidationError,
}


def error_from_response(status: int, body: dict[str, Any]) -> ControlPlaneError:
    envelope = body.get("error") or {}
    code = envelope.get("code", "http_error")
    message = envelope.get("message", "Unexpected server error")
    details = envelope.get("details") or {}
    cls = _CODE_MAP.get(code) or _STATUS_MAP.get(status, ControlPlaneError)
    return cls(code, message, status=status, details=details)
