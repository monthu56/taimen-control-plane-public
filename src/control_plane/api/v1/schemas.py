"""HTTP contract: request/response schemas (camelCase over the wire)."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from control_plane.domain.work_item import MAX_COMMENT_BODY_LENGTH
from control_plane.infrastructure.db.models import Base


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


# --- Requests -----------------------------------------------------------------


class IamIdentitySpec(ApiModel):
    """A federated identity as IAM names it: issuer, IAM tenant, IAM principal."""

    issuer: str = Field(min_length=1, max_length=2000)
    iam_tenant_id: uuid.UUID
    iam_principal_id: uuid.UUID


class BootstrapRequest(ApiModel):
    tenant_slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    tenant_name: str = Field(min_length=1, max_length=200)
    admin_display_name: str = Field(min_length=1, max_length=200)
    # One tenant, one UUID across IAM, the Control Plane and the platform core
    # (superproject ADR-0030): the installer passes the IAM tenant id here.
    tenant_id: uuid.UUID | None = None
    # Binds the admin principal to a federated identity in the same
    # transaction, so an IAM-only deployment gets its first administrator
    # without a hand-written SQL row (ADR-0053).
    iam_binding: IamIdentitySpec | None = None


class IamBindingUpsertRequest(IamIdentitySpec):
    permissions: list[str] = Field(min_length=1)


class PrincipalCreateRequest(ApiModel):
    kind: str
    display_name: str = Field(min_length=1, max_length=200)
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApiKeyCreateRequest(ApiModel):
    permissions: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class DelegationCreateRequest(ApiModel):
    human_principal_id: uuid.UUID
    agent_principal_id: uuid.UUID
    permissions: list[str] = Field(default_factory=list)
    starts_at: datetime | None = None
    expires_at: datetime | None = None


class HarnessBlock(ApiModel):
    """Harness registration announced at session open (control-harness/<n>)."""

    type: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    version: str = Field(default="", max_length=100)
    protocol_version: str = Field(default="1", max_length=20)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    hostname: str | None = Field(default=None, max_length=255)
    environment: dict[str, Any] = Field(default_factory=dict)


class SessionOpenRequest(ApiModel):
    client_name: str = Field(min_length=1, max_length=200)
    client_version: str = Field(default="", max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    on_behalf_of: uuid.UUID | None = None
    ttl_seconds: int | None = None
    harness: HarnessBlock | None = None


class SessionHeartbeatRequest(ApiModel):
    ttl_seconds: int | None = None


class TaskRequirementsSpec(ApiModel):
    roles: list[str] = Field(default_factory=list, max_length=50)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    skills: list[str] = Field(default_factory=list, max_length=50)


# --- work graph documents (CP-ADR-0062) -----------------------------------------
# Typed here for the contract and the OpenAPI document; the domain
# (domain/work_graph.py) re-validates the cross-field rules, because an
# approval outcome or a future rule engine writes the same documents without
# passing through HTTP.


class WorkExternalRefSpec(ApiModel):
    system: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=512)
    url: str | None = Field(default=None, min_length=1, max_length=2048)


class WorkEvidenceSpec(ApiModel):
    """A pointer to one fact: an observation, an artifact, an external object
    or the context pack an executor was given (CP-ADR-0064)."""

    kind: Literal["observation", "artifact", "external", "context_pack"]
    observation_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None
    context_pack_id: uuid.UUID | None = None
    external_ref: WorkExternalRefSpec | None = None
    # Key of the acceptance check this fact speaks to, if any.
    check: str | None = Field(default=None, min_length=1, max_length=63)
    note: str | None = Field(default=None, min_length=1, max_length=1000)


class WorkOriginSpec(ApiModel):
    """Why a work item (or goal) exists; recorded once, never rewritten."""

    kind: Literal["human", "harness", "rule", "parent", "process", "external"]
    ref: str | None = Field(default=None, min_length=1, max_length=512)
    rule_id: str | None = Field(default=None, min_length=1, max_length=200)
    evidence: list[WorkEvidenceSpec] = Field(default_factory=list, max_length=50)


class AcceptanceCheckSpec(ApiModel):
    """One declared check, executed by the verification stage (CP-ADR-0067).

    On a task, ``spec`` follows the grammar of ``kind``: ``deterministic`` —
    ``{skill: name@version, inputs?, expect?}`` or ``{artifact: {type,
    mediaTypes?, content?}}``; ``external_state`` —
    ``{event?}``; ``human`` — ``{approver? | approverRole?}``; ``llm_judge`` —
    as ``human`` plus ``rubric?``. A spec outside it is
    ``422 invalid_acceptance_spec``. On a goal it is not interpreted.
    """

    key: str = Field(min_length=1, max_length=63)
    kind: Literal["deterministic", "external_state", "human", "llm_judge"]
    description: str = Field(min_length=1, max_length=2000)
    spec: dict[str, Any] | None = None


def work_document(value: ApiModel | Sequence[ApiModel] | None) -> Any:
    """A typed work-graph spec as the camelCase JSON document the domain stores."""
    if value is None:
        return None
    if not isinstance(value, ApiModel):
        return [work_document(item) for item in value]
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


class TaskCreateRequest(ApiModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    priority: str = "medium"
    # None means "the initial status declared by the task type" (ADR-0048).
    status: str | None = Field(default=None, min_length=1, max_length=64)
    type_id: uuid.UUID | None = None
    type_key: str | None = Field(default=None, min_length=1, max_length=63)
    type_version: int | None = Field(default=None, ge=1)
    owner_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    # v0.8: validated against the field_schema of the selected type version.
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    start_date: datetime | None = None
    due_date: datetime | None = None
    parent_task: str | None = Field(default=None, min_length=1, max_length=100)
    requirements: TaskRequirementsSpec | None = None
    # M1.1 work graph (CP-ADR-0062). Without origin, the core derives it:
    # "parent" when parentTask is given, else from the writer's principal kind.
    goal_id: uuid.UUID | None = None
    origin: WorkOriginSpec | None = None
    acceptance: list[AcceptanceCheckSpec] = Field(default_factory=list, max_length=50)
    evidence: list[WorkEvidenceSpec] = Field(default_factory=list, max_length=200)


class TaskUpdateRequest(ApiModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    # A status KEY of the task type's lifecycle. The system status category is
    # never accepted from a client — it is derived from the key.
    status: str | None = Field(default=None, min_length=1, max_length=64)
    owner_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    # v0.8: whole-document replace (a merge could never remove a key); the
    # dates are nullable, so an explicit null clears a planned date.
    custom_fields: dict[str, Any] | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    requirements: TaskRequirementsSpec | None = None
    # CP-ADR-0062: goalId null unlinks; acceptance and evidence are
    # whole-document replaces. There is no origin here on purpose: where the
    # work came from is not editable.
    goal_id: uuid.UUID | None = None
    acceptance: list[AcceptanceCheckSpec] | None = Field(default=None, max_length=50)
    evidence: list[WorkEvidenceSpec] | None = Field(default=None, max_length=200)
    claim_id: uuid.UUID | None = None
    fencing_token: int | None = None


class TaskCompleteRequest(ApiModel):
    claim_id: uuid.UUID | None = None
    fencing_token: int | None = None


class ClaimTaskRequest(ApiModel):
    session_id: uuid.UUID
    ttl_seconds: int | None = None
    intent: str = Field(default="", max_length=500)


class ClaimHeartbeatRequest(ApiModel):
    ttl_seconds: int | None = None


class ClaimReleaseRequest(ApiModel):
    reason: str = Field(default="released", min_length=1, max_length=200)


class ClaimReclaimRequest(ApiModel):
    session_id: uuid.UUID
    ttl_seconds: int | None = None
    intent: str = Field(default="", max_length=500)


class ContextQueryRequest(ApiModel):
    """Working-context request. The server resolves and authorizes every
    scope inside the caller's tenant before any memory-provider call."""

    query: str = Field(default="", max_length=2000)
    task: str | None = None  # id or publicId
    run_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    include_subprojects: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    include_memory: bool = True
    anchors: list[str] = Field(default_factory=list, max_length=10)
    # Memory compilation strategy (MEM-ADR-016/019): "briefing" builds the standing
    # context of the principal without a query (TAI-ADR-0031 §6).
    strategy: str | None = Field(
        default=None, pattern=r"^(semantic|exact|graph|hybrid|context|briefing)$"
    )
    # Point-in-time recall (TAI-ADR-0042): passed to Memory only when set.
    as_of: AwareDatetime | None = None


_GRAPH_NAME = r"^[a-z][a-z0-9_]{0,62}$"


class RecallRequest(ApiModel):
    """Pull from the knowledge graph (``cp_recall``, CP-ADR-0064).

    Exactly one of ``anchor`` (an identifier: a key, an alias or text an
    ``idPattern`` matches) and ``query`` (text to extract identifiers from).
    ``relations`` are followed from the anchors, each ``depth`` steps in
    ``direction``, at most ``limit`` new entities per step. The namespaces come
    from ``task``/``workspaceId``, never from the client. The pack in the
    answer is cut to ``budgetTokens`` (``omitted`` counts the rest).
    """

    anchor: str | None = Field(default=None, min_length=1, max_length=300)
    kind: str | None = Field(default=None, pattern=_GRAPH_NAME)
    query: str | None = Field(default=None, min_length=1, max_length=2000)
    kinds: list[Annotated[str, Field(pattern=_GRAPH_NAME)]] = Field(
        default_factory=list, max_length=20
    )
    relations: list[Annotated[str, Field(pattern=_GRAPH_NAME)]] = Field(
        default_factory=list, max_length=10
    )
    direction: Literal["in", "out", "both"] = "both"
    depth: int = Field(default=1, ge=1, le=5)
    limit: int = Field(default=20, ge=1, le=200)
    as_of: AwareDatetime | None = None
    task: str | None = None
    workspace_id: uuid.UUID | None = None
    budget_tokens: int = Field(default=3_000, ge=1, le=32_000)


class ObservationExternalRef(ApiModel):
    """The observed object in its own system (issue, alert, commit...)."""

    system: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=512)
    url: str | None = Field(default=None, min_length=1, max_length=2048)


class ObservationCreateRequest(ApiModel):
    """Explicit remember: only intentional, externalized knowledge — never
    hidden reasoning, raw prompts or terminal history."""

    kind: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=65_536)
    data: dict[str, Any] | None = None
    assertions: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    task: str | None = None  # id or publicId
    run_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    # External observations (CP-ADR-0057): where the fact was seen, how to
    # recognise a repeat of it, and which earlier observation it replaces.
    source: str | None = Field(default=None, min_length=1, max_length=128)
    dedup_key: str | None = Field(default=None, min_length=1, max_length=512)
    observed_at: AwareDatetime | None = None
    supersedes: uuid.UUID | None = None
    external_ref: ObservationExternalRef | None = None


class ObservationRecordedOut(ApiModel):
    id: uuid.UUID
    event_id: uuid.UUID
    kind: str
    recorded_at: datetime
    # True when (source, dedupKey) matched an existing observation (HTTP 200).
    deduplicated: bool = False


# Memory's snapshot limits (``domain.reconcile``: MAX_SOURCE_LEN, MAX_SCOPE_LEN,
# MAX_SNAPSHOT_ID_LEN, ``reconcile_max_items``).
KNOWLEDGE_SOURCE_MAX = 200
KNOWLEDGE_SCOPE_MAX = 200
KNOWLEDGE_SNAPSHOT_ID_MAX = 200
KNOWLEDGE_MAX_ITEMS = 20_000


class KnowledgeSnapshotRequest(ApiModel):
    """A connector's snapshot of one source (CP-ADR-0060), forwarded as-is.

    Namespace and visibility scopes are computed by the core from
    ``workspaceId``; a client that sends them gets 400 like any unknown field.
    Entities and relations are opaque here: Memory validates them against the
    pack. Bounds mirror Memory's ``parse_snapshot`` so that an oversized
    document is refused here, not after a round trip."""

    workspace_id: uuid.UUID
    # Optional as in Memory (``pack: str = ""``): without it Memory checks kinds
    # against the namespace's catalog only.
    pack: str | None = Field(default=None, min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=KNOWLEDGE_SOURCE_MAX)
    # The snapshot's scope within its source: a string, not a memory namespace.
    scope: str | None = Field(default=None, max_length=KNOWLEDGE_SCOPE_MAX)
    snapshot_id: str = Field(min_length=1, max_length=KNOWLEDGE_SNAPSHOT_ID_MAX)
    observed_at: AwareDatetime
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=KNOWLEDGE_MAX_ITEMS)
    relations: list[dict[str, Any]] = Field(default_factory=list, max_length=KNOWLEDGE_MAX_ITEMS)

    @model_validator(mode="after")
    def _bounded_items(self) -> "KnowledgeSnapshotRequest":
        if len(self.entities) + len(self.relations) > KNOWLEDGE_MAX_ITEMS:
            raise ValueError(
                f"entities and relations together must not exceed {KNOWLEDGE_MAX_ITEMS} items"
            )
        return self

    def snapshot_document(self) -> dict[str, Any]:
        """The snapshot as Memory reads it: camelCase fields, no ``workspaceId``,
        ``null`` fields omitted."""
        return self.model_dump(
            mode="json", by_alias=True, exclude={"workspace_id"}, exclude_none=True
        )


class WorkspaceKnowledgePacksRequest(ApiModel):
    packs: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=64)
    strict: bool = False


# --- Responses ----------------------------------------------------------------


class TenantOut(ApiModel):
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime


class PrincipalOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    display_name: str
    status: str
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime


class RoleHolderOut(ApiModel):
    """A principal holding a role in a workspace (``GET /roles/{id}/principals``)."""

    id: uuid.UUID
    kind: str
    display_name: str
    status: str


class ApiKeyOut(ApiModel):
    id: uuid.UUID
    principal_id: uuid.UUID
    key_prefix: str
    permissions: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreatedOut(ApiKeyOut):
    # The full key is returned exactly once, at creation time. An idempotent
    # replay of the same request returns key=null (secrets are never stored).
    key: str | None


class IamBindingOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    issuer: str
    iam_tenant_id: uuid.UUID
    iam_principal_id: uuid.UUID
    permissions: list[str]
    status: str
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DelegationOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    human_principal_id: uuid.UUID
    agent_principal_id: uuid.UUID
    permissions: list[str]
    starts_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class SessionOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    on_behalf_of_id: uuid.UUID | None
    delegation_id: uuid.UUID | None
    status: str
    client_name: str
    client_version: str
    control_level: str
    harness_type: str | None
    harness_version: str | None
    protocol_version: str | None
    harness_capabilities: list[str] | None
    hostname: str | None
    environment: dict[str, Any] | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    started_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    ended_at: datetime | None


class TaskVerificationSummaryOut(ApiModel):
    id: uuid.UUID
    status: str
    attempt: int
    updated_at: datetime


class TaskOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    public_id: str
    workspace_id: uuid.UUID | None
    # v0.5: derived from the workspace tree at read time, never stored.
    project_id: uuid.UUID | None = None
    # v0.8: the type this task was created against, denormalized for readers.
    type_id: uuid.UUID
    type_key: str = ""
    type_version: int = 0
    title: str
    description: str
    # The tenant's own lifecycle key; core branches on the category next to it.
    status: str
    system_status_category: str
    priority: str
    owner_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    # v0.8: tenant-defined fields and planned dates (ADR-0049).
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    start_date: datetime | None = None
    due_date: datetime | None = None
    # M1.1 work graph (CP-ADR-0062).
    goal_id: uuid.UUID | None = None
    origin: dict[str, Any] = Field(default_factory=dict)
    acceptance: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    version: int
    claim_epoch: int
    active_claim_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    # M1.6 (CP-ADR-0067): the newest verification attempt, in brief; null for
    # a task never handed in with acceptance checks.
    verification: TaskVerificationSummaryOut | None = None


class TaskVerificationOut(ApiModel):
    """One attempt of the verification stage (CP-ADR-0067).

    ``checks`` — the acceptance the attempt runs, as it was when it opened;
    ``results`` — per check run so far: ``{key, kind, status, evidence,
    reason, message?}``; ``cursor`` — the index of the check it is at. The
    completer's credential snapshot stays internal.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    attempt: int
    status: str
    trigger: str
    trigger_ref: str | None
    authority_principal_id: uuid.UUID
    checks: list[dict[str, Any]]
    results: list[dict[str, Any]]
    cursor: int
    skill_invocation_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    next_check_at: datetime | None
    started_at: datetime
    finished_at: datetime | None
    updated_at: datetime


class ClaimOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    session_id: uuid.UUID
    holder_id: uuid.UUID
    status: str
    fencing_token: int
    intent: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None
    release_reason: str | None


class EventOut(ApiModel):
    """One journal event. ``sequence`` is an identifier/audit field; the
    replay cursor is the opaque ``cursor`` attached by the endpoint."""

    sequence: int
    id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: str = Field(serialization_alias="type")
    entity_type: str
    entity_id: uuid.UUID
    actor_id: uuid.UUID | None
    session_id: uuid.UUID | None
    correlation_id: str
    causation_id: str | None
    request_id: str
    # v0.5 trace correlator (X-Run-Id); null on pre-v0.5 events (ADR-0039).
    trace_run_id: str | None
    # IAM identity of the actor (CP-ADR-0055); null for legacy keys and old events.
    iam_actor_id: uuid.UUID | None = None
    # Workspace of the event's entity; null at tenant level and on events older
    # than CP-ADR-0068.
    workspace_id: uuid.UUID | None = None
    # Version of the payload schema in the event catalog (CP-ADR-0068).
    schema_version: int = 1
    payload: dict[str, Any]
    occurred_at: datetime


class BootstrapOut(ApiModel):
    tenant: TenantOut
    admin_principal: PrincipalOut
    api_key: ApiKeyCreatedOut
    iam_binding: IamBindingOut | None = None


class ClaimWithTaskOut(ApiModel):
    claim: ClaimOut
    task: TaskOut


class PageOut(ApiModel):
    items: list[dict[str, Any]]
    next_cursor: str | None


class EventPageOut(ApiModel):
    """Journal page: ``nextCursor`` is always present (echoes the input when
    nothing new is stable) so followers can poll without decoding cursors."""

    items: list[dict[str, Any]]
    next_cursor: str
    has_more: bool


class ErrorDetail(ApiModel):
    code: str
    message: str
    details: dict[str, Any]
    request_id: str


class ErrorEnvelope(ApiModel):
    error: ErrorDetail


def dump[M: ApiModel](model_cls: type[M], obj: Base, **extra: Any) -> dict[str, Any]:
    """Serialize an ORM object through its response schema to a JSON-safe dict."""
    model = model_cls.model_validate(obj, from_attributes=True)
    data = model.model_dump(mode="json", by_alias=True)
    data.update(extra)
    return data


def page_body(items: list[dict[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "nextCursor": next_cursor}


# --- v0.2 Organization Model --------------------------------------------------


_SLUG_FIELD = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")


class WorkspaceCreateRequest(ApiModel):
    slug: str = _SLUG_FIELD
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parent_id: uuid.UUID | None = None
    # v0.5: node type; omitted means the tenant's system default type.
    type_id: uuid.UUID | None = None
    type_key: str | None = Field(default=None, min_length=1, max_length=63)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class WorkspaceUpdateRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    slug: str | None = Field(
        default=None, min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    type_id: uuid.UUID | None = None
    type_key: str | None = Field(default=None, min_length=1, max_length=63)
    custom_fields: dict[str, Any] | None = None


class WorkspaceMoveRequest(ApiModel):
    new_parent_id: uuid.UUID | None = None


class WorkspaceMemberRequest(ApiModel):
    principal_id: uuid.UUID


class RoleCreateRequest(ApiModel):
    slug: str = _SLUG_FIELD
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    workspace_id: uuid.UUID | None = None


class RoleUpdateRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class RoleAssignRequest(ApiModel):
    role_id: uuid.UUID
    workspace_id: uuid.UUID | None = None


class ScopeRevokeRequest(ApiModel):
    workspace_id: uuid.UUID | None = None


class CapabilityCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class CapabilityAssignRequest(ApiModel):
    capability_id: uuid.UUID
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillRegisterRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="1.0.0", min_length=1, max_length=50)
    description: str = ""
    # Optional with a contract: then it is contract.implementation.protocol.
    protocol: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    # ADR-0056 §1: contract v1; without it the version is a catalog entry only.
    side_effects: str | None = None
    risk_level: str | None = None
    contract: dict[str, Any] | None = None


class SkillUpdateRequest(ApiModel):
    description: str | None = None
    config: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    status: str | None = None


class SkillInvokeRequest(ApiModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    run_id: uuid.UUID | None = None
    # Basis for an external_write skill (ADR-0056 §4): an approved gate
    # approval on the same task.
    approval_id: uuid.UUID | None = None


class SkillInvocationClaimRequest(ApiModel):
    protocols: list[str] = Field(min_length=1, max_length=10)
    local_entrypoints: list[str] = Field(default_factory=list, max_length=500)
    # What the executor may reach and which tokens it issues (ADR-0056
    # amendment M2.2, D): http/mcp calls outside them are never handed out.
    http_origins: list[str] = Field(default_factory=list, max_length=100)
    mcp_endpoints: list[str] = Field(default_factory=list, max_length=100)
    audiences: list[str] = Field(default_factory=list, max_length=100)
    session_id: uuid.UUID | None = None
    lease_seconds: int | None = Field(default=None, gt=0)
    # Take this invocation and no other (the executor of an execution-typed
    # task claims the call its own run created).
    invocation_id: uuid.UUID | None = None


class SkillInvocationCancelRequest(ApiModel):
    reason: str = Field(default="cancelled", min_length=1, max_length=500)


class SkillInvocationHeartbeatRequest(ApiModel):
    fencing_token: int
    lease_seconds: int | None = Field(default=None, gt=0)
    # Required when the lease was claimed under a session (ADR-0056 amendment).
    session_id: uuid.UUID | None = None


class SkillInvocationCompleteRequest(ApiModel):
    fencing_token: int
    output: dict[str, Any]
    cost: dict[str, Any] | None = None
    session_id: uuid.UUID | None = None


class SkillInvocationError(ApiModel):
    code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    message: str = Field(default="", max_length=4000)
    retryable: bool = False
    details: dict[str, Any] | None = None


class SkillInvocationFailRequest(ApiModel):
    fencing_token: int
    error: SkillInvocationError
    session_id: uuid.UUID | None = None


class SkillAssignRequest(ApiModel):
    skill_id: uuid.UUID
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelationCreateRequest(ApiModel):
    to_task: str = Field(min_length=1, max_length=100)
    type: str


class RunStartRequest(ApiModel):
    claim_id: uuid.UUID
    fencing_token: int
    input: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    max_duration_seconds: int | None = Field(default=None, gt=0)
    max_actions: int | None = Field(default=None, gt=0)


class RunSucceedRequest(ApiModel):
    output: dict[str, Any] | None = None
    complete_task: bool = True


class RunFailRequest(ApiModel):
    failure_reason: str = Field(default="failed", min_length=1, max_length=2000)
    output: dict[str, Any] | None = None


class RunCancelRequest(ApiModel):
    reason: str = Field(default="cancelled", min_length=1, max_length=500)


class RunSuspendRequest(ApiModel):
    reason: str = Field(default="waiting_approval", min_length=1, max_length=200)
    waiting_for_approval_id: uuid.UUID | None = None


class HandoffCheckpointData(ApiModel):
    summary: str = Field(min_length=1, max_length=10_000)
    next_steps: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


class HandoffCheckpointRequest(ApiModel):
    kind: str = Field(default="handoff", pattern=r"^handoff$")
    data: HandoffCheckpointData


class RunHandoffRequest(ApiModel):
    reason: str = Field(default="human_harness_handoff", pattern=r"^human_harness_handoff$")
    checkpoint: HandoffCheckpointRequest


class RunRequestCancelRequest(ApiModel):
    reason: str = Field(default="", max_length=500)


class RunControlMessageCreateRequest(ApiModel):
    operation: str
    causal_position: str = Field(min_length=1, max_length=500)
    directive: str | None = Field(default=None, max_length=10_000)
    reason: str = Field(default="", max_length=2000)
    expected_run_version: int = Field(ge=1)


class RunControlMessageAcknowledgeRequest(ApiModel):
    status: str
    claim_id: uuid.UUID
    fencing_token: int
    expected_run_version: int = Field(ge=1)
    expected_message_version: int = Field(ge=1)
    safe_boundary: str | None = Field(default=None, max_length=500)
    reason: str = Field(default="", max_length=2000)


class ChildGrantRequest(ApiModel):
    """Requested ceiling. An omitted field inherits; ``[]`` grants nothing."""

    permissions: list[str] | None = Field(default=None, max_length=100)
    capabilities: list[str] | None = Field(default=None, max_length=100)
    skills: list[str] | None = Field(default=None, max_length=100)


class ChildRunLaunchRequest(ApiModel):
    correlation_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=10_000)
    priority: str = "medium"
    workspace_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    grant: ChildGrantRequest | None = None
    cancellation_policy: str | None = None
    expires_in_seconds: int | None = Field(default=None, ge=1)


class ChildRunRevokeRequest(ApiModel):
    reason: str = Field(default="", max_length=500)
    cancel_child: bool = False


class CheckpointCreateRequest(ApiModel):
    kind: str = Field(min_length=1, max_length=100)
    data: dict[str, Any] = Field(default_factory=dict)


class RunActionCreateRequest(ApiModel):
    action: str = Field(min_length=1, max_length=200)
    status: str = "completed"
    skill: str | None = Field(default=None, max_length=250)
    external_reference: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunActionFinishRequest(ApiModel):
    status: str
    external_reference: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ManifestCompileRequest(ApiModel):
    """What a harness may DECLARE about its own runtime (HRS-2).

    Identity, project policy, tool policy and budgets are absent by design:
    they are computed by the server. Extra keys are ACCEPTED here and forwarded
    to the domain validator on purpose — that way an attempt to supply
    ``identity`` is answered with ``server_authoritative_section``, which says
    what the rule is, instead of a generic "unexpected field".
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="allow",
    )

    reason: str = Field(default="recompile", pattern=r"^(recompile|provider_fallback)$")
    worker_profile: dict[str, Any] | None = None
    execution_backend: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    redaction: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None

    def declared_sections(self) -> dict[str, Any]:
        declared = {
            "workerProfile": self.worker_profile,
            "executionBackend": self.execution_backend,
            "model": self.model,
            "redaction": self.redaction,
        }
        return {
            **(self.model_extra or {}),
            **{key: value for key, value in declared.items() if value is not None},
        }


class ManifestEphemeralRequest(ApiModel):
    kind: str = Field(pattern=r"^(steering|warning|budget_warning|note)$")
    summary: str = Field(min_length=1, max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreateRequest(ApiModel):
    type: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    task: str | None = Field(default=None, max_length=100)
    run_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    uri: str | None = Field(default=None, max_length=2000)
    content: dict[str, Any] | None = None
    # An upload of PUT /artifact-contents (CP-ADR-0072 §2); excludes uri and content.
    content_ref: str | None = Field(default=None, min_length=1, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    supersedes_artifact_id: uuid.UUID | None = None


class ArtifactPurgeContentRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class TaskCommentCreateRequest(ApiModel):
    """The author is NOT a field here: it comes from the credential (ADR-0050)."""

    body: str = Field(min_length=1, max_length=MAX_COMMENT_BODY_LENGTH)
    run_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None


class TaskCommentUpdateRequest(ApiModel):
    body: str = Field(min_length=1, max_length=MAX_COMMENT_BODY_LENGTH)


class AttentionFeedbackRequest(ApiModel):
    """A verdict on an item of the caller's attention list (CP-ADR-0071)."""

    verdict: Literal["useful", "not_needed"]
    comment: str | None = Field(default=None, max_length=1000)


class ApprovalRequestRequest(ApiModel):
    task: str | None = Field(default=None, max_length=100)
    artifact_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    required_role_id: uuid.UUID | None = None
    assigned_principal_id: uuid.UUID | None = None
    comment: str = Field(default="", max_length=4000)
    gate: bool = False


class ApprovalDecisionRequest(ApiModel):
    comment: str | None = Field(default=None, max_length=4000)


class WorkspaceOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    parent_id: uuid.UUID | None
    type_id: uuid.UUID
    slug: str
    name: str
    description: str
    custom_fields: dict[str, Any]
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class WorkspaceMemberOut(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    principal_id: uuid.UUID
    created_at: datetime


class RoleOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    slug: str
    name: str
    description: str
    version: int
    created_at: datetime
    updated_at: datetime


class CapabilityOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str
    created_at: datetime


class SkillOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    version: str
    description: str
    protocol: str
    config: dict[str, Any]
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    side_effects: str | None = None
    risk_level: str | None = None
    contract: dict[str, Any] | None = None
    status: str
    row_version: int
    created_at: datetime
    updated_at: datetime


class SkillExecutionOut(ApiModel):
    """What an executor needs to run a claimed call — the contract, nothing else.

    The catalog ``config`` (addresses, headers) stays behind ``org.read``
    (ADR-0056 amendment, item 8); ``skills.execute`` alone does not reveal it.
    """

    id: uuid.UUID
    name: str
    version: str
    protocol: str
    side_effects: str | None = None
    risk_level: str | None = None
    contract: dict[str, Any] | None = None


class SkillInvocationOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    skill_id: uuid.UUID
    inputs: dict[str, Any]
    requested_by_kind: str = Field(exclude=True)
    requested_by_ref: str = Field(exclude=True)
    authority_principal_id: uuid.UUID
    authorization_basis: dict[str, Any] | None
    idempotency_key: str | None
    status: str
    attempt: int
    max_attempts: int
    available_at: datetime
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    cost: dict[str, Any] | None
    fencing_token: int
    executor_principal_id: uuid.UUID | None
    executor_session_id: uuid.UUID | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskRelationOut(ApiModel):
    id: uuid.UUID
    from_task_id: uuid.UUID
    to_task_id: uuid.UUID
    relation_type: str = Field(serialization_alias="type")
    created_by_principal_id: uuid.UUID
    created_at: datetime


class RunOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    claim_id: uuid.UUID
    principal_id: uuid.UUID
    session_id: uuid.UUID
    fencing_token: int
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    failure_reason: str | None
    cancel_requested_at: datetime | None
    cancel_requested_by: uuid.UUID | None
    max_duration_seconds: int | None
    max_actions: int | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    # CP-ADR-0066: the executor instructions the run was started under.
    instructions_hash: str | None = None
    instructions_refs: dict[str, Any] | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class CheckpointOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    created_by_principal_id: uuid.UUID
    seq: int
    kind: str
    data: dict[str, Any]
    created_at: datetime


class RunActionOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    principal_id: uuid.UUID
    session_id: uuid.UUID | None
    skill_id: uuid.UUID | None
    seq: int
    action: str
    status: str
    external_reference: str | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime


class RunControlMessageOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    seq: int
    operation: str
    status: str
    causal_position: str
    directive: str | None
    reason: str
    safe_boundary: str | None
    idempotency_key: str
    requested_by_principal_id: uuid.UUID
    acknowledged_by_principal_id: uuid.UUID | None
    request_id: str
    correlation_id: str
    causation_id: str | None
    version: int
    accepted_at: datetime
    resolved_at: datetime | None


class RunControlMessageResultOut(ApiModel):
    control_message: RunControlMessageOut
    run_version: int


class ArtifactOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    created_by_principal_id: uuid.UUID
    type: str
    name: str
    uri: str | None
    content: dict[str, Any] | None
    supersedes_artifact_id: uuid.UUID | None
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    # Content in the store (CP-ADR-0072 §1): none | stored | purged; the other
    # fields are null without stored (or once stored) content.
    content_state: str
    size_bytes: int | None
    media_type: str | None
    sha256: str | None
    # Version of the registered artifact type it was checked against
    # (CP-ADR-0072 §6); null for an unregistered type.
    type_version: int | None
    created_at: datetime


class ArtifactContentOut(ApiModel):
    """An upload waiting for an artifact to reference it (CP-ADR-0072 §2)."""

    content_ref: str
    size_bytes: int
    media_type: str
    sha256: str
    expires_at: datetime


class TaskCommentOut(ApiModel):
    """One reply in a work item's thread (ADR-0050)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    # Derived from the authenticated context at write time, never from a body.
    author_principal_id: uuid.UUID
    body: str
    run_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
    edited_at: datetime | None


class TaskCommentRevisionOut(ApiModel):
    """A superseded version of a comment — the audit trail of an edit."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    comment_id: uuid.UUID
    task_id: uuid.UUID
    version: int
    body: str
    author_principal_id: uuid.UUID
    created_at: datetime
    superseded_at: datetime
    superseded_by: uuid.UUID


class ApprovalOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    task_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    requested_by_principal_id: uuid.UUID
    status: str
    gate: bool
    required_role_id: uuid.UUID | None
    assigned_principal_id: uuid.UUID | None
    decision_by_principal_id: uuid.UUID | None
    decision_at: datetime | None
    comment: str
    version: int
    # null | pending | deferred | executed | failed (CP-ADR-0061)
    outcome_status: str | None
    created_at: datetime
    updated_at: datetime


class ApprovalOutcomeActionOut(ApiModel):
    index: int
    action: str
    status: str
    attempts: int
    result: dict[str, Any]
    error: dict[str, Any] | None
    # A reaction: the index of the invokeSkill it reacts to, and to which
    # ending (onSuccess | onFailure).
    reacts_to: int | None = None
    when: str | None = None


class ApprovalOutcomeOut(ApiModel):
    """A decision's declared outcome and what happened to each action."""

    approval_id: uuid.UUID
    outcome: str | None
    outcome_status: str | None
    # Attempts that died of an unexpected error, the last such error and when
    # the worker looks again (pending/deferred only).
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    actions: list[ApprovalOutcomeActionOut]


# --- v0.5 Project Model -------------------------------------------------------

_TYPE_KEY_FIELD = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class WorkspaceTypeCreateRequest(ApiModel):
    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    field_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_child_types: list[str] | None = Field(default=None, max_length=100)


class WorkspaceTypeUpdateRequest(ApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    field_schema: dict[str, Any] | None = None
    allowed_child_types: list[str] | None = Field(default=None, max_length=100)


class WorkspaceTypeOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    display_name: str
    description: str
    field_schema: dict[str, Any]
    allowed_child_types: list[str]
    is_system: bool
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class ProjectTemplateCreateRequest(ApiModel):
    """Creating a version, never editing one: the server allocates `version`."""

    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    field_schema: dict[str, Any] = Field(default_factory=dict)
    lifecycle_schema: dict[str, Any] | None = None
    default_config: dict[str, Any] = Field(default_factory=dict)
    default_views: list[Any] = Field(default_factory=list, max_length=50)
    governance_schema: dict[str, Any] = Field(default_factory=dict)
    memory_defaults: dict[str, Any] = Field(default_factory=dict)


class ProjectTemplateOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    version: int
    display_name: str
    description: str
    field_schema: dict[str, Any]
    lifecycle_schema: dict[str, Any]
    default_config: dict[str, Any]
    default_views: list[Any]
    governance_schema: dict[str, Any]
    memory_defaults: dict[str, Any]
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class TaskTypeCreateRequest(ApiModel):
    """Creating a version, never editing one: the server allocates `version`."""

    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    field_schema: dict[str, Any] = Field(default_factory=dict)
    lifecycle_schema: dict[str, Any] | None = None
    # ADR-0056 §3: {skill, version, inputs} — tasks of this type are executed
    # by one skill invocation.
    execution: dict[str, Any] | None = None
    # CP-ADR-0061: outcomes of a decided gate approval on a task of this version.
    approval_schema: dict[str, Any] = Field(default_factory=dict)
    # CP-ADR-0064: where a task of this version takes its knowledge context from.
    context_schema: dict[str, Any] = Field(default_factory=dict)
    # CP-ADR-0066: how to execute a task of this version, Markdown up to 16 KiB
    # (size and credentials are checked by the command, with a stable code).
    instructions: str = ""
    # CP-ADR-0061 amendment 2026-09-25: work core files once a task of this
    # version is completed ({"onComplete": {"when", "actions"}}).
    completion_schema: dict[str, Any] = Field(default_factory=dict)
    # CP-ADR-0072 §7: artifacts a task of this version takes in and hands on
    # ({"inputs": [...], "outputs": [...]}); checked against the registry.
    artifact_schema: dict[str, Any] = Field(default_factory=dict)


class TaskTypeOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    version: int
    display_name: str
    description: str
    field_schema: dict[str, Any]
    lifecycle_schema: dict[str, Any]
    execution: dict[str, Any] | None = None
    approval_schema: dict[str, Any]
    context_schema: dict[str, Any]
    instructions: str = ""
    completion_schema: dict[str, Any] = Field(default_factory=dict)
    artifact_schema: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ArtifactTypeCreateRequest(ApiModel):
    """Creating a version, never editing one: the server allocates `version`.

    ``mediaTypes`` and ``maxBytes`` are checked by the command, so an empty
    list or a ceiling above ``CP_ARTIFACT_MAX_BYTES`` is ``invalid_artifact_type``
    like every other defect of the definition (CP-ADR-0072 §6).
    """

    key: str = _TYPE_KEY_FIELD
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    media_types: list[Any]
    # Omitted: the global ceiling at the time of creation.
    max_bytes: int | None = None


class ArtifactTypeOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    version: int
    display_name: str
    description: str
    metadata_schema: dict[str, Any]
    media_types: list[str]
    max_bytes: int
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ProjectCreateRequest(ApiModel):
    """Attach to an existing workspace, or create workspace + profile at once."""

    workspace_id: uuid.UUID | None = None
    workspace_slug: str | None = Field(
        default=None, min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    workspace_name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_workspace_id: uuid.UUID | None = None
    workspace_type_key: str | None = Field(default=None, min_length=1, max_length=63)
    template_id: uuid.UUID | None = None
    template_key: str | None = Field(default=None, min_length=1, max_length=63)
    template_version: int | None = Field(default=None, ge=1)
    status_key: str | None = Field(default=None, min_length=1, max_length=64)
    owner_principal_id: uuid.UUID | None = None
    start_date: datetime | None = None
    target_date: datetime | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateRequest(ApiModel):
    owner_principal_id: uuid.UUID | None = None
    clear_owner: bool = False
    start_date: datetime | None = None
    target_date: datetime | None = None
    custom_fields: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    template_id: uuid.UUID | None = None
    template_key: str | None = Field(default=None, min_length=1, max_length=63)
    template_version: int | None = Field(default=None, ge=1)


class ProjectTransitionRequest(ApiModel):
    status_key: str = Field(min_length=1, max_length=64)
    comment: str = Field(default="", max_length=1000)


class ProjectOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    template_id: uuid.UUID
    status_key: str
    system_status_category: str
    owner_principal_id: uuid.UUID | None
    start_date: datetime | None
    target_date: datetime | None
    custom_fields: dict[str, Any]
    settings: dict[str, Any]
    active_config_revision_id: uuid.UUID | None
    status: str
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    # Derived at read time from the workspace tree and the template.
    parent_project_id: uuid.UUID | None = None
    template_key: str | None = None
    template_version: int | None = None
    active_config_revision: int | None = None


class ConfigRevisionCreateRequest(ApiModel):
    config: dict[str, Any] = Field(default_factory=dict)
    comment: str = Field(default="", max_length=1000)


class ConfigRevisionOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    revision: int
    config: dict[str, Any]
    validation: dict[str, Any]
    comment: str
    created_by: uuid.UUID
    created_at: datetime
    activated_at: datetime | None


class ExternalReferenceCreateRequest(ApiModel):
    external_system: str = Field(min_length=1, max_length=512)
    external_type: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalReferenceRegisterRequest(ExternalReferenceCreateRequest):
    """Generic registration: the entity is named in the body, not in the path.

    ``entityId`` is a reference rather than a strict UUID — a task may be named
    by its public id, which is what an importer carrying legacy identifiers has
    in hand.
    """

    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=128)


class ExternalReferenceOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    external_system: str
    external_type: str
    external_id: str
    metadata_json: dict[str, Any] = Field(serialization_alias="metadata")
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AdapterRedriveRequest(ApiModel):
    reason: str = Field(default="operator_redrive", min_length=1, max_length=500)


class AdapterRebuildRequest(ApiModel):
    cursor: str | None = None
    reason: str = Field(default="operator_rebuild", min_length=1, max_length=500)


class JournalArchiveRequest(ApiModel):
    """Move (or delete) journal history up to a safe horizon."""

    before_seconds: int | None = Field(default=None, ge=0)
    max_events: int | None = Field(default=None, ge=1, le=100_000)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Malformed request"},
    401: {"model": ErrorEnvelope, "description": "Missing or invalid credentials"},
    403: {"model": ErrorEnvelope, "description": "Insufficient permissions"},
    404: {"model": ErrorEnvelope, "description": "Not found"},
    409: {"model": ErrorEnvelope, "description": "Concurrency conflict"},
    422: {"model": ErrorEnvelope, "description": "Domain validation failed"},
}


# --- M1.1 work graph: goals (CP-ADR-0062) -------------------------------------


class GoalCreateRequest(ApiModel):
    title: str = Field(min_length=1, max_length=500)
    desired_state: str = Field(default="", max_length=10_000)
    criteria: list[AcceptanceCheckSpec] = Field(default_factory=list, max_length=50)
    owner_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    parent_goal_id: uuid.UUID | None = None
    # Omitted: derived from the writer's principal kind (human / harness).
    created_from: WorkOriginSpec | None = None


class GoalUpdateRequest(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    desired_state: str | None = Field(default=None, max_length=10_000)
    criteria: list[AcceptanceCheckSpec] | None = Field(default=None, max_length=50)
    # null clears the owner / detaches from the parent goal.
    owner_id: uuid.UUID | None = None
    status: Literal["active", "achieved", "abandoned"] | None = None
    parent_goal_id: uuid.UUID | None = None


class GoalOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    title: str
    desired_state: str
    criteria: list[dict[str, Any]]
    owner_id: uuid.UUID | None
    status: str
    created_from: dict[str, Any]
    parent_goal_id: uuid.UUID | None
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


# --- M1.3 work derivation rules (CP-ADR-0063) -----------------------------------
# The four documents are typed loosely here on purpose: their grammar (a
# closed expression language, templates, cross-document rules) is validated by
# domain/work_rules.py, which also serves writers that do not pass through HTTP.


class RuleCreateRequest(ApiModel):
    key: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    workspace_id: uuid.UUID | None = None
    goal_id: uuid.UUID | None = None
    trigger: dict[str, Any]
    # Omitted: always true.
    condition: dict[str, Any] | bool | None = None
    interpretation: dict[str, Any] | None = None
    action: dict[str, Any]
    status: Literal["enabled", "disabled"] = "enabled"


class RuleUpdateRequest(ApiModel):
    description: str | None = Field(default=None, max_length=2000)
    trigger: dict[str, Any] | None = None
    condition: dict[str, Any] | bool | None = None
    # null removes the interpretation; goalId null unlinks the goal.
    interpretation: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    goal_id: uuid.UUID | None = None


class RuleOut(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    key: str
    description: str
    version: int
    status: str
    trigger: dict[str, Any]
    condition: Any
    interpretation: dict[str, Any] | None
    action: dict[str, Any]
    # Whose authority the rule acts with, and since when it sees facts.
    authority_principal_id: uuid.UUID | None
    enabled_at: datetime | None
    next_run_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class RuleEvaluationOut(ApiModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    rule_version: int
    trigger_ref: str
    trigger_event_id: uuid.UUID | None
    status: str
    result: dict[str, Any]
    evidence: list[Any]
    skill_invocation_id: uuid.UUID | None
    created_task_ids: list[Any]
    error: dict[str, Any] | None
    next_check_at: datetime | None
    created_at: datetime
    updated_at: datetime
