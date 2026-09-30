"""Official Control Plane client SDK (control-harness/2).

``ControlPlaneClient``/``ControlPlaneError`` are the only names — the
codename-era package, aliases and env vars were removed in v0.5 (ADR-0040,
docs/migration-v0.5.md).
"""

from control_plane_client.client import ControlPlaneClient, HeartbeatRunner
from control_plane_client.config import (
    LegacyProjectConfigError,
    ProjectConfig,
    find_project_config,
    write_project_config,
)
from control_plane_client.credentials import (
    CredentialProvider,
    StaticCredential,
    delete_api_key,
    resolve_api_key,
    resolve_credential,
    store_api_key,
)
from control_plane_client.errors import (
    ApprovalRequiredError,
    AuthenticationError,
    BudgetExceededError,
    ClaimConflictError,
    ConflictError,
    ControlPlaneError,
    IdempotencyConflictError,
    NotEligibleError,
    NotFoundError,
    PermissionDeniedError,
    RunNotActiveError,
    SessionExpiredError,
    SkillUnavailableError,
    StaleClaimError,
    TaskNotReadyError,
    TransportError,
    ValidationError,
    VersionConflictError,
    is_transient,
)
from control_plane_client.iam import IamCredential, IamCredentialError

__all__ = [
    "ApprovalRequiredError",
    "AuthenticationError",
    "BudgetExceededError",
    "ClaimConflictError",
    "ConflictError",
    "ControlPlaneClient",
    "ControlPlaneError",
    "CredentialProvider",
    "HeartbeatRunner",
    "IamCredential",
    "IamCredentialError",
    "IdempotencyConflictError",
    "LegacyProjectConfigError",
    "NotEligibleError",
    "NotFoundError",
    "PermissionDeniedError",
    "ProjectConfig",
    "RunNotActiveError",
    "SessionExpiredError",
    "SkillUnavailableError",
    "StaleClaimError",
    "StaticCredential",
    "TaskNotReadyError",
    "TransportError",
    "ValidationError",
    "VersionConflictError",
    "delete_api_key",
    "find_project_config",
    "is_transient",
    "resolve_api_key",
    "resolve_credential",
    "store_api_key",
    "write_project_config",
]
