"""Domain errors.

Application commands raise these; the API layer maps them onto the single
error envelope with honest HTTP status codes.
"""

from typing import Any


class DomainError(Exception):
    """Base class: a business-rule violation with a stable machine-readable code."""

    http_status: int = 422

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ValidationError(DomainError):
    """Domain-level validation failure (422)."""

    http_status = 422


class BadRequestError(DomainError):
    """The request is well-formed JSON but violates a declared contract (400).

    Used where the contract is data rather than API shape — e.g. skill inputs
    checked against the skill's own JSON Schema (ADR-0056).
    """

    http_status = 400


class NotFoundError(DomainError):
    http_status = 404

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("not_found", message, details=details)


class ConflictError(DomainError):
    """Concurrent conflict (409): claims, versions, idempotency reuse, etc."""

    http_status = 409


class AuthenticationError(DomainError):
    http_status = 401

    def __init__(self, message: str = "Missing or invalid credentials") -> None:
        super().__init__("invalid_credentials", message)


class DependencyUnavailableError(DomainError):
    """An external decision could not be obtained (503).

    Distinct from a denial on the merits: the right was not checked, not
    refused. Retrying makes sense, but serving the request now does not —
    enforcement has to fail closed.
    """

    http_status = 503

    def __init__(
        self,
        message: str = "Authorization decision is temporarily unavailable",
        *,
        code: str = "decision_unavailable",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, details=details)


class UpstreamError(DomainError):
    """A dependency the request is proxied to answered with a failure (502)."""

    http_status = 502


class AuthorizationError(DomainError):
    http_status = 403

    def __init__(
        self,
        message: str = "Insufficient permissions",
        *,
        code: str = "permission_denied",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, details=details)
