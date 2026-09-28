"""Product-neutral Control Plane MCP adapter for human and managed harnesses.

Architecture (ADR-0016/0017):

    Codex / Claude Code / other harness ──MCP/stdio──▶ SDK ──▶ Control Plane

The MCP server is a STATELESS ADAPTER: it holds no business invariants, no
authoritative state, and gets no special trust — the Control Plane re-checks
authorization, eligibility, readiness, leases and fencing on every command.
Tools mirror user workflows (discover → inspect → claim → run → artifact →
complete), not REST routes. Side-effectful tools (claim/complete/approve/...)
are separate explicit tools so the human harness surfaces them as explicit
actions; the server never auto-claims anything.

The only process-local state is the current session/claim/run ids — a cache
of "what this harness window is working on", recoverable at any time via
``cp_context`` after a restart.

Credentials come from the environment/keychain/credential file (see
control_plane_client.credentials) — never from MCP config or the repository.
"""

import asyncio
import json
import mimetypes
import os
import re
import tempfile
from collections.abc import Mapping
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from control_plane_agent.context_pack import render_graph_pack
from control_plane_agent.inputs import safe_file_name
from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    HeartbeatRunner,
    NotFoundError,
    SessionExpiredError,
    find_project_config,
    resolve_credential,
)
from control_plane_mcp.environment import MCP_RUN_ENV, MCP_TASK_ENV

HARNESS_CAPABILITIES = [
    "tasks.interactive",
    "artifacts.publish",
    "approvals.interactive",
    "resume",
    "checkpoints",
    "active_turn_control.v1",
    "child_run_handle.v1",
    "skills.protocol.mcp",
    "skills.protocol.local",
]
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
MUTATING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

#: Writes that record what happened rather than decide what happens. An
#: executor nested inside a run — an agent driven by a harness adapter — needs
#: these to leave evidence of its own work, and asking for an approval is a
#: request, not a decision.
EVIDENCE_TOOLS = frozenset(
    {
        "cp_checkpoint",
        "cp_record_action",
        "cp_create_artifact",
        "cp_remember",
        "cp_comment",
        "cp_request_approval",
    }
)


async def withheld_tool_names() -> list[str]:
    """Tools a nested executor must not call, derived from this registry.

    The list is computed, not written down, because a written-down list is a
    copy of this module that ages badly: a tool added tomorrow would silently
    stay callable. Anything not annotated read-only and not in
    :data:`EVIDENCE_TOOLS` is withheld — an unannotated tool is treated as
    authoritative, so forgetting an annotation closes a door instead of opening
    one.

    This narrows what an agent is asked to do; it is not a security boundary.
    The same credential can reach the API by other means, and the real ceiling
    is a child grant computed by the server (ADR-0046).
    """
    tools = await mcp.list_tools()
    return sorted(
        tool.name
        for tool in tools
        if not (tool.annotations and tool.annotations.read_only_hint)
        and tool.name not in EVIDENCE_TOOLS
    )


def _package_version() -> str:
    try:
        return importlib_metadata.version("control-plane")
    except importlib_metadata.PackageNotFoundError:  # pragma: no cover - source checkout
        return "0+unknown"


def _harness_metadata() -> tuple[str, str, str]:
    harness_type = os.environ.get("CONTROL_PLANE_HARNESS_TYPE", "mcp-client")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", harness_type) is None:
        raise ControlPlaneError(
            "invalid_harness_configuration",
            "CONTROL_PLANE_HARNESS_TYPE must be a lowercase client identifier",
        )
    package_version = _package_version()
    return (
        harness_type,
        os.environ.get("CONTROL_PLANE_HARNESS_VERSION", package_version),
        os.environ.get("CONTROL_PLANE_HARNESS_CLIENT_NAME", "control-plane-mcp"),
    )


mcp = MCPServer(
    name="control-plane",
    instructions=(
        "Control Plane integration. Tasks here are authoritative work items. "
        "Typical flow: cp_whoami / cp_context, cp_list_tasks or cp_list_work, "
        "cp_get_task, then "
        "inspect, then — only after the human chose a task — cp_claim_task, "
        "cp_start_run, do the work, cp_create_artifact, cp_complete_run. Never "
        "claim or complete without an explicit user decision. If a tool reports "
        "stale_claim, ownership was lost: stop writing and re-read cp_context. "
        "Task statuses are declared by the tenant's work item type, not by this "
        "server: read the transitions in cp_get_task before proposing a status "
        "change, and filter by systemStatusCategory when the meaning matters "
        "more than the label."
    ),
)


class _State:
    """Process-local working-state cache (recoverable, never authoritative)."""

    client: ControlPlaneClient | None = None
    session_id: str | None = None
    claim_id: str | None = None
    fencing_token: int | None = None
    run_id: str | None = None
    task_ref: str | None = None
    # v0.5: the project the harness is currently working inside. Advisory
    # local state, like every other field here — the server always re-derives
    # ownership from the workspace tree.
    project_id: str | None = None
    heartbeats: HeartbeatRunner | None = None
    session_lock: asyncio.Lock | None = None


STATE = _State()

#: Media types whose content cp_get_artifact_content also returns as text.
_TEXT_MEDIA_TYPES = re.compile(
    r"^(text/.+|application/(json|xml|yaml|x-yaml|toml|[a-z0-9.+-]+\+(json|xml)))$"
)
_INLINE_TEXT_BYTES = 64 * 1024


def adopt_run_from_environment(environ: Mapping[str, str] | None = None) -> None:
    """Take the task and run a harness adapter started this process for.

    Under a runner the agent's MCP server would otherwise know neither, and
    what the agent records would belong to no run (``environment.py``). The
    claim is not adopted: the lifecycle stays with the runner.
    """
    env = os.environ if environ is None else environ
    task = env.get(MCP_TASK_ENV, "").strip()
    run = env.get(MCP_RUN_ENV, "").strip()
    if task and run:
        STATE.task_ref = task
        STATE.run_id = run


async def _start_heartbeats() -> None:
    """Keep session + current claim leases alive while a claim is held.

    The MCP process is long-lived and runs an asyncio loop, but Claude drives
    tools intermittently — without a background heartbeat a claim TTL would
    expire mid-work and the task could be taken over (protocol §6). Failure is
    not hidden: _claim_heartbeat_lost() surfaces it on the next tool call.
    """
    await _stop_heartbeats()
    if STATE.client is None or STATE.session_id is None:
        return
    runner = HeartbeatRunner(
        STATE.client,
        session_id=STATE.session_id,
        claim_id=STATE.claim_id,
        interval_seconds=60.0,
    )
    runner.start()
    STATE.heartbeats = runner


async def _stop_heartbeats() -> None:
    if STATE.heartbeats is not None:
        await STATE.heartbeats.stop()
        STATE.heartbeats = None


def _claim_heartbeat_lost() -> dict[str, Any] | None:
    """A dead heartbeat means the lease was lost — report it, don't hide it."""
    if STATE.heartbeats is not None and STATE.heartbeats.error is not None:
        err = STATE.heartbeats.error
        return {
            "error": err.code,
            "message": f"Lease heartbeat failed: {err.message}",
            "hint": (
                "Ownership of the task may be lost. Call cp_context and, if the "
                "claim is gone, stop authoritative writes and consult the user."
            ),
        }
    return None


def _server_url() -> str:
    env = os.environ.get("CONTROL_PLANE_SERVER")
    if env:
        return env.rstrip("/")
    config = find_project_config()
    if config is not None:
        return config.server
    raise ControlPlaneError(
        "not_configured",
        "No Control Plane server configured: set CONTROL_PLANE_SERVER or create "
        ".control-plane/config.json (control-plane init --server https://...)",
    )


def _client() -> ControlPlaneClient:
    if STATE.client is None:
        server = _server_url()
        credential = resolve_credential(server)
        if credential is None:
            raise ControlPlaneError(
                "not_authenticated",
                f"No credentials for {server}: run `iam auth login` for an IAM "
                f"identity, or `control-plane login --server {server}` for an "
                "API key",
            )
        STATE.client = ControlPlaneClient(server, credential)
    return STATE.client


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _error(exc: ControlPlaneError) -> str:
    payload = {"error": exc.code, "message": exc.message, "details": exc.details}
    if exc.code in ("stale_claim", "task_already_claimed", "run_not_active"):
        payload["hint"] = (
            "Ownership of this task is not (or no longer) yours. Do not retry the "
            "write; call cp_context, tell the user, and decide together."
        )
    return _dump(payload)


async def _ensure_session() -> str:
    """Open (or reuse) the harness session for this MCP process.

    Only a DOMAIN failure (expired/closed/gone) justifies a new session; a
    transport blip must propagate, or a flaky network would silently orphan
    the claims attached to the still-live session.
    """
    if STATE.session_lock is None:
        # Constructed lazily inside the server event loop. The assignment is
        # synchronous, so concurrent first callers observe the same lock.
        STATE.session_lock = asyncio.Lock()
    async with STATE.session_lock:
        client = _client()
        if STATE.session_id is not None:
            try:
                await client.heartbeat_session(STATE.session_id)
                return STATE.session_id
            except (SessionExpiredError, NotFoundError):
                await _stop_heartbeats()
                STATE.session_id = None  # expired/closed: open a fresh one
        config = find_project_config()
        harness_type, harness_version, client_name = _harness_metadata()
        environment: dict[str, Any] = {}
        if config is not None and config.repository:
            environment["repository"] = config.repository
        session = await client.open_session(
            client_name=client_name,
            client_version=_package_version(),
            harness_type=harness_type,
            harness_version=harness_version,
            capabilities=HARNESS_CAPABILITIES,
            environment=environment,
        )
        STATE.session_id = str(session["id"])
        return STATE.session_id


# --- identity & discovery -----------------------------------------------------


@mcp.tool(
    description="Who am I in the Control Plane: tenant, principal, permissions and Project focus.",
    annotations=READ_ONLY,
)
async def cp_whoami() -> str:
    try:
        client = _client()
        session_id = await _ensure_session()
        context = await client.get_context(session_id=session_id)
        config = find_project_config()
        project_ref = STATE.project_id or (config.project if config else None)
        project = await client.get_project(project_ref) if project_ref else None
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(
        {
            "tenant": context["tenant"],
            "principal": context["principal"],
            "permissions": context["permissions"],
            "projectFocus": project,
        }
    )


@mcp.tool(
    description=(
        "Full Control Plane context: active sessions, my claims and runs (including "
        "suspended ones), roles, skills, pending approvals, event cursor. Call "
        "this at the start of work and after any restart/reconnect."
    ),
    annotations=READ_ONLY,
)
async def cp_context() -> str:
    try:
        client = _client()
        session_id = await _ensure_session()
        context = await client.get_context(session_id=session_id)
        config = find_project_config()
        project_ref = STATE.project_id or (config.project if config else None)
        context["projectFocus"] = await client.get_project(project_ref) if project_ref else None
    except ControlPlaneError as exc:
        return _error(exc)
    context["local"] = {
        "sessionId": STATE.session_id,
        "claimId": STATE.claim_id,
        "runId": STATE.run_id,
        "taskRef": STATE.task_ref,
    }
    lost = _claim_heartbeat_lost()
    if lost is not None:
        context["leaseWarning"] = lost
    return _dump(context)


@mcp.tool(
    description=(
        "Working context for a task: authoritative current state (task, claim, "
        "runs, artifacts, approvals) PLUS durable memory recalled by the "
        "external context engine — previous findings, decisions, related facts "
        "with provenance. THE tool to continue work in a fresh session without "
        "the old conversation. memoryStatus tells whether memory was available; "
        "operational state is always authoritative over remembered facts. "
        "mode='briefing' builds the standing briefing of the principal without a "
        "query (my claims, runs, approvals, recent observations, active facts)."
    ),
    annotations=READ_ONLY,
)
async def cp_get_context(
    query: str = "",
    task: str = "",
    project: str = "",
    max_tokens: int = 0,
    anchors: list[str] | None = None,
    mode: str = "",
) -> str:
    try:
        context = await _client().get_working_context(
            query=query,
            anchors=anchors,
            strategy=mode or None,
            task_ref=task or STATE.task_ref,
            run_id=STATE.run_id if not task else None,
            project_id=project or STATE.project_id,
            max_tokens=max_tokens or None,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(context)


@mcp.tool(
    description=(
        "Recall from the organization's knowledge graph (contracts, documents, "
        "people, regulations — whatever the workspace's domain packs describe): "
        "give ONE of anchor (an identifier you have: an endpoint 'GET /runs/{id}', "
        "an ADR number, a table, a file path) or query (text; identifiers are "
        "extracted from it). relations are followed from the anchors (e.g. "
        "['calls'] with direction='in' — who calls this endpoint), depth steps "
        "each; as_of (ISO 8601 with offset) reads the graph as it was then. Same "
        "visibility as your working context. Returns 'text' — the same rendering "
        "as the task context in your prompt, within budget_tokens — and the "
        "anchors used; include_pack=true adds the raw typed pack. Read-only."
    ),
    annotations=READ_ONLY,
)
async def cp_recall(
    anchor: str = "",
    query: str = "",
    relations: list[str] | None = None,
    depth: int = 1,
    as_of: str = "",
    direction: str = "both",
    kind: str = "",
    kinds: list[str] | None = None,
    limit: int = 20,
    task: str = "",
    budget_tokens: int = 3000,
    include_pack: bool = False,
) -> str:
    try:
        result = await _client().recall(
            anchor=anchor or None,
            query=query or None,
            kind=kind or None,
            kinds=kinds,
            relations=relations,
            direction=direction,
            depth=depth,
            limit=limit,
            as_of=as_of or None,
            task_ref=task or STATE.task_ref,
            budget_tokens=budget_tokens,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    note = "Recall" + (f" as of {result['asOf']}" if result.get("asOf") else "")
    pack = result.pop("pack", None)
    # The server cut the pack to the budget; the text holds to it as well, with
    # room on top for the heading and the notice of the fence.
    budget_chars = max(1, min(budget_tokens, 32_000)) * 4 + 1024
    result["text"] = render_graph_pack(pack, note=note, budget_chars=budget_chars)
    if include_pack:
        result["pack"] = pack
    return _dump(result)


@mcp.tool(
    description=(
        "Remember an explicit finding/decision/constraint/note durably, so ANY "
        "future session (yours or another harness) can recall it. Submit only "
        "intentional externalized knowledge the user would agree to persist — "
        "never hidden reasoning, raw prompts or terminal history. Attaches to "
        "the current task/run automatically unless task is given. Optional assertions "
        "use Memory entity/fact shapes; CP controls identity, namespace and permissions. "
        "For a fact seen in an external system pass source (lower-case system name, "
        "required with dedup_key/external_ref), dedup_key (a repeat returns the "
        "existing observation), observed_at (ISO 8601 with offset), supersedes (id "
        "of the previous observation of the same object) and external_ref "
        "({system, id, url})."
    ),
    annotations=MUTATING,
)
async def cp_remember(
    content: str,
    kind: str = "finding",
    task: str = "",
    assertions: list[dict[str, Any]] | None = None,
    source: str = "",
    dedup_key: str = "",
    observed_at: str = "",
    supersedes: str = "",
    external_ref: dict[str, Any] | None = None,
) -> str:
    try:
        session_id = await _ensure_session()
        result = await _client().remember(
            kind=kind,
            content=content,
            assertions=assertions,
            task_ref=task or STATE.task_ref,
            run_id=STATE.run_id if not task else None,
            session_id=session_id,
            source=source or None,
            dedup_key=dedup_key or None,
            observed_at=observed_at or None,
            supersedes=supersedes or None,
            external_ref=external_ref,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(result)


@mcp.tool(
    description=(
        "List tasks available to me right now (eligible, ready, unclaimed). "
        "Advisory: claiming may still fail if someone gets there first. "
        "assigned_to_me narrows the page to work addressed to this Principal — "
        "an unassigned task is not 'mine', because nobody decided it was."
    ),
    annotations=READ_ONLY,
)
async def cp_list_work(
    workspace_id: str | None = None,
    include_descendants: bool = False,
    project_id: str | None = None,
    include_subprojects: bool = False,
    assigned_to_me: bool = False,
    limit: int = 20,
) -> str:
    try:
        config = find_project_config()
        page = await _client().list_available_work(
            limit=limit,
            workspace_id=workspace_id,
            include_descendants=include_descendants,
            project_id=project_id or STATE.project_id or (config.project if config else None),
            include_subprojects=include_subprojects,
            assigned_to_me=assigned_to_me,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    items = [
        {
            "id": t["id"],
            "publicId": t["publicId"],
            "title": t["title"],
            "priority": t["priority"],
            "status": t["status"],
            "systemStatusCategory": t.get("systemStatusCategory"),
            "typeKey": t.get("typeKey"),
            "workspaceId": t["workspaceId"],
            "projectId": t.get("projectId"),
        }
        for t in page["items"]
    ]
    return _dump({"items": items, "nextCursor": page["nextCursor"]})


@mcp.tool(
    description=(
        "Inspect one task: full record, claimability diagnosis and the statuses it "
        "may move to next. Status keys belong to the tenant's task type, so read "
        "transitions before proposing a status change — each target says whether "
        "cp_update_task walks that edge (route 'update') or completion does "
        "(route 'complete'). A task with acceptance checks is done only once they "
        "pass: 'verification' is its newest verification attempt (status "
        "running/waiting_*/passed/failed/cancelled), or null."
    ),
    annotations=READ_ONLY,
)
async def cp_get_task(task: str) -> str:
    try:
        client = _client()
        record = await client.get_task(task)
        claimability = await client.get_claimability(task)
    except ControlPlaneError as exc:
        return _error(exc)
    body: dict[str, Any] = {
        "task": record,
        "claimability": claimability,
        # CP-ADR-0067: completion of a task with acceptance is a verification
        # attempt first; an older server has no such field.
        "verification": record.get("verification"),
    }
    try:
        body["transitions"] = await client.get_task_transitions(task)
    except ControlPlaneError as exc:
        # A server that predates the projection must not make the whole
        # inspection fail: the record and its claimability are still true.
        body["transitions"] = {"error": exc.code, "message": exc.message}
    return _dump(body)


@mcp.tool(
    description=(
        "List the work item types this tenant declares: each one owns its own "
        "status vocabulary and transition graph. Read-only — types are managed "
        "outside the harness."
    ),
    annotations=READ_ONLY,
)
async def cp_list_task_types(
    key: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> str:
    params: dict[str, Any] = {}
    for name, value in (("key", key), ("status", status), ("cursor", cursor), ("limit", limit)):
        if value is not None:
            params[name] = value
    try:
        page = await _client().list_task_types(**params)
    except ControlPlaneError as exc:
        return _error(exc)
    items = [
        {
            "id": t["id"],
            "key": t["key"],
            "version": t["version"],
            "displayName": t["displayName"],
            "status": t["status"],
            "initialStatus": t["lifecycleSchema"].get("initialStatus"),
            "statuses": [
                {
                    "key": s.get("key"),
                    "displayName": s.get("displayName", s.get("key")),
                    "systemStatusCategory": s.get("category"),
                }
                for s in t["lifecycleSchema"].get("statuses", [])
            ],
            # Full document via cp_get_task_type (CP-ADR-0061).
            "declaresApprovalOutcomes": bool(t.get("approvalSchema")),
            "declaresContextProfile": bool(t.get("contextSchema")),
        }
        for t in page["items"]
    ]
    return _dump({"items": items, "nextCursor": page["nextCursor"]})


@mcp.tool(
    description=(
        "Inspect one work item type version by id: its full lifecycle (statuses, "
        "system categories, declared transitions), field schema and approvalSchema — "
        "the actions core executes when a gate approval on a task of this version is "
        "decided (approved/rejected: ensureWork, completeTask, comment, transition, "
        "invokeSkill) — and contextSchema, where its tasks take their knowledge "
        "context from (anchors, traverse, asOf). Read-only."
    ),
    annotations=READ_ONLY,
)
async def cp_get_task_type(type_id: str) -> str:
    try:
        return _dump(await _client().get_task_type(type_id))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Create an authoritative Task. This has lasting workflow consequences: "
        "call only after the human explicitly confirms the title, scope and ownership. "
        "When parent_task is supplied, Task and parent relation commit atomically. "
        "type_key selects the work item type (see cp_list_task_types) and with it the "
        "status vocabulary; omitted, the tenant's system type applies. The task starts "
        "in the type's initial status. custom_fields must satisfy the field schema of "
        "that type (cp_get_task_type shows it) and must not carry secrets; start_date "
        "and due_date are ISO-8601 instants. goal_id links the task to the Goal it "
        "serves (cp_list_goals). origin says why the work exists and is never editable "
        "afterwards: {kind: human|harness|rule|parent|process|external, ref?, ruleId?, "
        "evidence?}; kind 'rule' needs ruleId and at least one evidence item. Omitted, "
        "the server derives it. acceptance is a list of checks {key, kind: "
        "deterministic|external_state|human|llm_judge, description, spec?}; evidence "
        "items are {kind: observation|artifact|external, observationId | artifactId | "
        "externalRef {system, id, url?}, check?, note?} and must name facts that exist."
    ),
    annotations=MUTATING,
)
async def cp_create_task(
    title: str,
    description: str = "",
    priority: str = "medium",
    type_key: str | None = None,
    type_version: int | None = None,
    workspace_id: str | None = None,
    project_id: str | None = None,
    assignee_id: str | None = None,
    owner_id: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    parent_task: str | None = None,
    goal_id: str | None = None,
    origin: dict[str, Any] | None = None,
    acceptance: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> str:
    try:
        client = _client()
        config = find_project_config()
        focus = project_id or STATE.project_id or (config.project if config else None)
        if focus:
            project = await client.get_project(focus)
            project_workspace = str(project["workspaceId"])
            if workspace_id is not None and workspace_id != project_workspace:
                return _dump(
                    {
                        "error": "scope_mismatch",
                        "message": "workspace_id does not match the selected Project",
                    }
                )
            workspace_id = project_workspace
        task = await client.create_task(
            title=title,
            description=description,
            priority=priority,
            type_key=type_key,
            type_version=type_version,
            workspace_id=workspace_id,
            assignee_id=assignee_id,
            owner_id=owner_id,
            custom_fields=custom_fields,
            start_date=start_date,
            due_date=due_date,
            parent_task=parent_task,
            goal_id=goal_id,
            origin=origin,
            acceptance=acceptance,
            evidence=evidence,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(task)


@mcp.tool(
    description=(
        "List authoritative Tasks in any state, not only claimable ones. Status keys "
        "belong to the tenant's task type; filter by systemStatusCategory (backlog, "
        "active, blocked, terminal_success, terminal_cancelled) when the meaning "
        "matters more than the label. Date bounds (ISO-8601) are inclusive and only "
        "match tasks that carry that date. sort='dueDate' or 'startDate' reads the "
        "page soonest-first with undated tasks last; the default is newest-created "
        "first. A cursor belongs to the sort it was issued under."
    ),
    annotations=READ_ONLY,
)
async def cp_list_tasks(
    status: str | None = None,
    system_status_category: str | None = None,
    type_key: str | None = None,
    workspace_id: str | None = None,
    project_id: str | None = None,
    assignee_id: str | None = None,
    owner_id: str | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    sort: str | None = None,
    include_subprojects: bool = False,
    cursor: str | None = None,
    limit: int = 50,
) -> str:
    try:
        config = find_project_config()
        page = await _client().list_tasks(
            status=status,
            system_status_category=system_status_category,
            type_key=type_key,
            workspace_id=workspace_id,
            project_id=project_id or STATE.project_id or (config.project if config else None),
            assignee_id=assignee_id,
            owner_id=owner_id,
            due_from=due_from,
            due_to=due_to,
            start_from=start_from,
            start_to=start_to,
            sort=sort,
            include_subprojects=include_subprojects,
            cursor=cursor,
            limit=limit,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(page)


@mcp.tool(
    description=(
        "Update an authoritative Task using its exact expected_version. This mutation "
        "requires explicit human confirmation; version_conflict means re-read and ask again. "
        "A status must be one of the targets cp_get_task reports with route 'update'; "
        "targets with route 'complete' are reached by completing the task, not here. "
        "custom_fields REPLACES the whole document and is validated against the field "
        "schema of the task's type; use clear_start_date / clear_due_date to remove a "
        "planned date, since omitting one leaves it untouched. goal_id relinks the task "
        "to another Goal and clear_goal unlinks it; acceptance and evidence REPLACE their "
        "whole lists (read the task first and send the complete list). The origin of a "
        "task cannot be changed."
    ),
    annotations=MUTATING,
)
async def cp_update_task(
    task: str,
    expected_version: int,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    assignee_id: str | None = None,
    owner_id: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    clear_start_date: bool = False,
    clear_due_date: bool = False,
    goal_id: str | None = None,
    clear_goal: bool = False,
    acceptance: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> str:
    fields: dict[str, Any] = {}
    for name, value in (
        ("title", title),
        ("description", description),
        ("priority", priority),
        ("status", status),
        ("assignee_id", assignee_id),
        ("owner_id", owner_id),
        ("custom_fields", custom_fields),
        ("start_date", start_date),
        ("due_date", due_date),
        ("goal_id", goal_id),
        ("acceptance", acceptance),
        ("evidence", evidence),
    ):
        if value is not None:
            fields[name] = value
    for name, clear in (
        ("start_date", clear_start_date),
        ("due_date", clear_due_date),
        ("goal_id", clear_goal),
    ):
        if clear:
            if name in fields:
                return _dump(
                    {
                        "error": "invalid_request",
                        "message": f"{name} cannot be set and cleared in the same update",
                    }
                )
            fields[name] = None
    claim_id = STATE.claim_id if STATE.task_ref == task else None
    fencing_token = STATE.fencing_token if claim_id else None
    try:
        result = await _client().update_task(
            task,
            expected_version=expected_version,
            claim_id=claim_id,
            fencing_token=fencing_token,
            **fields,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(result)


@mcp.tool(
    description=(
        "Add a Task relation after the human confirms its direction and type. "
        "The server enforces tenant isolation and parent/dependency cycle checks."
    ),
    annotations=MUTATING,
)
async def cp_add_task_relation(from_task: str, to_task: str, relation_type: str) -> str:
    try:
        return _dump(
            await _client().add_task_relation(
                from_task, to_task=to_task, relation_type=relation_type
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description="Remove a Task relation only after explicit human confirmation.",
    annotations=MUTATING,
)
async def cp_remove_task_relation(task: str, relation_id: str) -> str:
    try:
        await _client().remove_task_relation(task, relation_id)
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump({"removed": True, "relationId": relation_id})


# --- goals (CP-ADR-0062) -------------------------------------------------------


@mcp.tool(
    description=(
        "Create a Goal: a desired state that work items serve (link tasks with "
        "cp_create_task goal_id). Lasting consequences: call only after the human "
        "confirms the title, the desired state and the owner. criteria are checks "
        "{key, kind: deterministic|external_state|human|llm_judge, description, spec?}. "
        "created_from uses the task origin shape {kind, ref?, ruleId?, evidence?} and "
        "is never editable; omitted, the server derives it."
    ),
    annotations=MUTATING,
)
async def cp_create_goal(
    title: str,
    desired_state: str = "",
    criteria: list[dict[str, Any]] | None = None,
    owner_id: str | None = None,
    workspace_id: str | None = None,
    parent_goal_id: str | None = None,
    created_from: dict[str, Any] | None = None,
) -> str:
    try:
        goal = await _client().create_goal(
            title=title,
            desired_state=desired_state,
            criteria=criteria,
            owner_id=owner_id,
            workspace_id=workspace_id,
            parent_goal_id=parent_goal_id,
            created_from=created_from,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(goal)


@mcp.tool(
    description=(
        "Update a Goal using its exact expected_version (cp_get_goal reports it). "
        "Lasting consequences: call only after the human confirms the change; "
        "version_conflict means re-read and ask again. status is active, achieved or "
        "abandoned (closing a goal is status achieved or abandoned; the core does not "
        "verify criteria). criteria REPLACES the whole list. clear_owner / clear_parent "
        "remove the owner / the parent goal. The workspace and created_from of a goal "
        "cannot be changed."
    ),
    annotations=MUTATING,
)
async def cp_update_goal(
    goal_id: str,
    expected_version: int,
    title: str | None = None,
    desired_state: str | None = None,
    criteria: list[dict[str, Any]] | None = None,
    status: str | None = None,
    owner_id: str | None = None,
    clear_owner: bool = False,
    parent_goal_id: str | None = None,
    clear_parent: bool = False,
) -> str:
    fields: dict[str, Any] = {}
    for name, value in (
        ("title", title),
        ("desired_state", desired_state),
        ("criteria", criteria),
        ("status", status),
        ("owner_id", owner_id),
        ("parent_goal_id", parent_goal_id),
    ):
        if value is not None:
            fields[name] = value
    for name, clear in (("owner_id", clear_owner), ("parent_goal_id", clear_parent)):
        if clear:
            if name in fields:
                return _dump(
                    {
                        "error": "invalid_request",
                        "message": f"{name} cannot be set and cleared in the same update",
                    }
                )
            fields[name] = None
    try:
        goal = await _client().update_goal(goal_id, expected_version=expected_version, **fields)
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(goal)


@mcp.tool(
    description=(
        "List Goals, newest first. status is active, achieved or abandoned; "
        "parent_goal_id lists the direct subgoals of one goal."
    ),
    annotations=READ_ONLY,
)
async def cp_list_goals(
    status: str | None = None,
    workspace_id: str | None = None,
    owner_id: str | None = None,
    parent_goal_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> str:
    try:
        page = await _client().list_goals(
            status=status,
            workspace_id=workspace_id,
            owner_id=owner_id,
            parent_goal_id=parent_goal_id,
            cursor=cursor,
            limit=limit,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    items = [
        {
            "id": g["id"],
            "title": g["title"],
            "status": g["status"],
            "ownerId": g["ownerId"],
            "workspaceId": g["workspaceId"],
            "parentGoalId": g["parentGoalId"],
            "criteriaCount": len(g.get("criteria") or []),
            "createdFromKind": (g.get("createdFrom") or {}).get("kind"),
        }
        for g in page["items"]
    ]
    return _dump({"items": items, "nextCursor": page["nextCursor"]})


@mcp.tool(
    description=(
        "Inspect one Goal: its desired state, criteria and origin, plus the first page "
        "of the work items that serve it (include_subgoals adds the work of its "
        "subgoals). Continue the work list with work_cursor."
    ),
    annotations=READ_ONLY,
)
async def cp_get_goal(
    goal_id: str,
    include_subgoals: bool = False,
    work_cursor: str | None = None,
    work_limit: int = 20,
) -> str:
    try:
        client = _client()
        goal = await client.get_goal(goal_id)
        work = await client.list_goal_work(
            goal_id, include_subgoals=include_subgoals, cursor=work_cursor, limit=work_limit
        )
    except ControlPlaneError as exc:
        return _error(exc)
    items = [
        {
            "id": t["id"],
            "publicId": t["publicId"],
            "title": t["title"],
            "status": t["status"],
            "systemStatusCategory": t.get("systemStatusCategory"),
            "goalId": t.get("goalId"),
            "originKind": (t.get("origin") or {}).get("kind"),
        }
        for t in work["items"]
    ]
    return _dump({"goal": goal, "work": {"items": items, "nextCursor": work["nextCursor"]}})


# --- work rules (CP-ADR-0063) -------------------------------------------------------


@mcp.tool(
    description=(
        "List work derivation rules, newest first: rules that file work on their own "
        "when observed facts match. status is enabled, disabled or archived (archived "
        "only when asked for); trigger_kind is observation, event or schedule."
    ),
    annotations=READ_ONLY,
)
async def cp_list_rules(
    status: str | None = None,
    workspace_id: str | None = None,
    key: str | None = None,
    trigger_kind: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> str:
    try:
        page = await _client().list_rules(
            status=status,
            workspace_id=workspace_id,
            key=key,
            trigger_kind=trigger_kind,
            cursor=cursor,
            limit=limit,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    items = [
        {
            "id": r["id"],
            "key": r["key"],
            "version": r["version"],
            "status": r["status"],
            "workspaceId": r["workspaceId"],
            "goalId": r["goalId"],
            "trigger": r["trigger"],
            "skill": (r.get("interpretation") or {}).get("skill"),
            "action": {"kind": r["action"]["kind"], "taskType": r["action"].get("taskType")},
        }
        for r in page["items"]
    ]
    return _dump({"items": items, "nextCursor": page["nextCursor"]})


@mcp.tool(
    description=(
        "Inspect one work derivation rule — its trigger, condition, interpretation "
        "skill and action — plus its latest evaluations: what fact it looked at, "
        "whether the condition held, the evidence and the work it filed. Explains "
        "a work item whose origin is kind=rule. Continue the history with "
        "evaluations_cursor; evaluation_status filters it."
    ),
    annotations=READ_ONLY,
)
async def cp_get_rule(
    rule_id: str,
    evaluation_status: str | None = None,
    evaluations_cursor: str | None = None,
    evaluations_limit: int = 20,
) -> str:
    try:
        client = _client()
        rule = await client.get_rule(rule_id)
        history = await client.list_rule_evaluations(
            rule_id,
            status=evaluation_status,
            cursor=evaluations_cursor,
            limit=evaluations_limit,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(
        {
            "rule": rule,
            "evaluations": {"items": history["items"], "nextCursor": history["nextCursor"]},
        }
    )


# --- comments -----------------------------------------------------------------


@mcp.tool(
    description=(
        "Read the discussion of a work item, oldest first. Comments are "
        "coordination — decisions, questions, hand-off notes — not work product; "
        "the deliverable itself is an Artifact. Pass the returned nextCursor back "
        "to this same tool to continue: the cursor belongs to this ordering and "
        "is rejected by other listings."
    ),
    annotations=READ_ONLY,
)
async def cp_list_comments(task: str, cursor: str | None = None, limit: int | None = None) -> str:
    params: dict[str, Any] = {}
    for name, value in (("cursor", cursor), ("limit", limit)):
        if value is not None:
            params[name] = value
    try:
        return _dump(await _client().list_task_comments(task, **params))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Append a comment to a work item after the human decides what to say. "
        "The author is this session's Principal and is never taken from the text, "
        "so an agent's reply is distinguishable from a person's. Never put "
        "credentials, raw prompts, transcripts or reasoning here — the server "
        "rejects credential-shaped text, and the rest belongs in an Artifact or "
        "nowhere. Optionally attach the run or artifact the comment is about; "
        "both must belong to the same task."
    ),
    annotations=MUTATING,
)
async def cp_comment(
    task: str, body: str, run_id: str | None = None, artifact_id: str | None = None
) -> str:
    try:
        return _dump(
            await _client().add_task_comment(
                task, body=body, run_id=run_id, artifact_id=artifact_id
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Correct one's OWN comment using its exact expected_version; only the "
        "author may edit, and the superseded text is kept as an auditable "
        "revision. version_conflict means re-read the comment and ask again."
    ),
    annotations=MUTATING,
)
async def cp_edit_comment(task: str, comment_id: str, body: str, expected_version: int) -> str:
    try:
        return _dump(
            await _client().edit_task_comment(
                task, comment_id, body=body, expected_version=expected_version
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


# --- claim / run lifecycle ----------------------------------------------------


@mcp.tool(
    description=(
        "Claim a task for the current user (exclusive lease + fencing token). "
        "Only call after the user explicitly chose this task."
    ),
    annotations=MUTATING,
)
async def cp_claim_task(task: str, intent: str = "") -> str:
    try:
        client = _client()
        # If a previous attempt committed server-side but its response was
        # lost (or the user simply retries), re-claiming would bump the
        # fencing epoch and invalidate our own in-flight run. Reuse the live
        # claim the server already attributes to us.
        context = await client.get_context()
        for held in context["activeClaims"]:
            if task in (held["taskId"], held["taskPublicId"]):
                # Keep the current run only if it belongs to THIS task;
                # otherwise it is leftover state pointing at other work.
                same_task = STATE.task_ref in (
                    None,
                    task,
                    held["taskId"],
                    held["taskPublicId"],
                )
                STATE.claim_id = held["id"]
                STATE.fencing_token = int(held["fencingToken"])
                STATE.task_ref = task
                # Heartbeat the session that actually backs this claim.
                STATE.session_id = held["sessionId"]
                if not same_task:
                    STATE.run_id = None
                await _start_heartbeats()
                return _dump({**held, "reused": True})
        session_id = await _ensure_session()
        claim = await client.claim_task(task, session_id, intent=intent)
    except ControlPlaneError as exc:
        return _error(exc)
    # A new claim replaces any prior working state (a stale run_id pointing at
    # a different task would make later complete/checkpoint act on the wrong run).
    STATE.claim_id = str(claim["id"])
    STATE.fencing_token = int(claim["fencingToken"])
    STATE.task_ref = task
    STATE.run_id = None
    await _start_heartbeats()
    return _dump(claim)


@mcp.tool(
    description="Release the current claim without completing the task.", annotations=MUTATING
)
async def cp_release_task(reason: str = "released") -> str:
    if STATE.claim_id is None:
        return _dump({"error": "no_local_claim", "message": "No claim held by this session."})
    try:
        claim = await _client().release_claim(STATE.claim_id, reason=reason)
    except ControlPlaneError as exc:
        return _error(exc)
    await _stop_heartbeats()
    STATE.claim_id = None
    STATE.fencing_token = None
    STATE.run_id = None
    STATE.task_ref = None  # a later cp_remember must not attach to a released task
    return _dump(claim)


@mcp.tool(
    description=(
        "Start an execution run under the current claim. Returns the run; use "
        "cp_get_run_context for the full working context (checkpoints of "
        "previous attempts, artifacts, approvals, skills)."
    ),
    annotations=MUTATING,
)
async def cp_start_run(
    max_duration_seconds: int | None = None, max_actions: int | None = None
) -> str:
    if STATE.claim_id is None or STATE.fencing_token is None or STATE.task_ref is None:
        return _dump({"error": "no_local_claim", "message": "Claim a task first."})
    try:
        run = await _client().start_run(
            STATE.task_ref,
            claim_id=STATE.claim_id,
            fencing_token=STATE.fencing_token,
            max_duration_seconds=max_duration_seconds,
            max_actions=max_actions,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    STATE.run_id = str(run["id"])
    return _dump(run)


@mcp.tool(description="Get a run by id (defaults to the current run).", annotations=READ_ONLY)
async def cp_get_run(run_id: str | None = None) -> str:
    target = run_id or STATE.run_id
    if target is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(await _client().get_run(target))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Full execution context of a run: task, claim, requirements, artifacts, "
        "checkpoints from previous attempts, pending approvals, usable skills, and "
        "'instructions' — how to do this task, as layers (platform contract, "
        "project, task type) with their hash."
    ),
    annotations=READ_ONLY,
)
async def cp_get_run_context(run_id: str | None = None) -> str:
    target = run_id or STATE.run_id
    if target is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(await _client().get_run_context(target))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Search the tools this principal may actually use, instead of loading a whole "
        "catalog into context. Returns a bounded page (name, version, protocol, one-line "
        "summary) plus the catalog/policy revisions it was computed from; an empty query "
        "returns the first page of everything available. Finding a tool here is not "
        "permission to run it — every invocation is authorized again server-side."
    ),
    annotations=READ_ONLY,
)
async def cp_search_tools(
    query: str = "",
    limit: int | None = None,
    cursor: str | None = None,
    run_id: str | None = None,
) -> str:
    try:
        return _dump(
            await _client().search_tools(
                query=query, run_id=run_id or STATE.run_id, limit=limit, cursor=cursor
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Full input schema of one tool, by id or name@version — load it right before "
        "using that tool. The schema is sanitized: connection config, defaults and vendor "
        "extensions are removed and the removed paths are listed in schemaRedactions. A "
        "tool outside the effective policy is reported as not found."
    ),
    annotations=READ_ONLY,
)
async def cp_describe_tool(tool: str, run_id: str | None = None) -> str:
    try:
        return _dump(await _client().describe_tool(tool, run_id=run_id or STATE.run_id))
    except ControlPlaneError as exc:
        return _error(exc)


#: Upper bound of how long cp_invoke_skill keeps the harness waiting.
MAX_SKILL_WAIT_SECONDS = 120.0


@mcp.tool(
    description=(
        "Full contract of one skill version, by id, name@version or name (newest active "
        "version): input/output JSON Schemas, sideEffects, riskLevel, requiredPermissions, "
        "timeout, retryPolicy, idempotency and implementation. A skill without a contract "
        "is a catalog entry and cannot be invoked."
    ),
    annotations=READ_ONLY,
)
async def cp_describe_skill(skill: str) -> str:
    try:
        return _dump(await _client().describe_skill(skill))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Invoke a skill through the Control Plane and wait a bounded time for the result. "
        "The call is attached to the current task/run when there is one. Returns the "
        "invocation: when status is still pending/running after waitSeconds, keep its id "
        "and read it later instead of invoking again. Pass idempotencyKey to make a retry "
        "safe. external_write skills need an approved gate approval (approvalId)."
    ),
    annotations=MUTATING,
)
async def cp_invoke_skill(
    skill: str,
    inputs: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    approval_id: str | None = None,
    wait_seconds: float = 20.0,
) -> str:
    try:
        client = _client()
        invocation = await client.invoke_skill(
            skill,
            inputs=inputs or {},
            idempotency_key=idempotency_key,
            task_id=STATE.task_ref if STATE.run_id is None else None,
            run_id=STATE.run_id,
            approval_id=approval_id,
        )
        wait = min(max(wait_seconds, 0.0), MAX_SKILL_WAIT_SECONDS)
        if wait > 0:
            invocation = await client.wait_skill_invocation(
                str(invocation["id"]), timeout_seconds=wait
            )
        return _dump(invocation)
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Send a durable queue/steer/redirect/request_cancel/force_cancel message to a Run. "
        "This changes authoritative execution state; ask the user before sending it."
    ),
    annotations=MUTATING,
)
async def cp_control_run(
    operation: str,
    causal_position: str,
    expected_run_version: int,
    directive: str | None = None,
    reason: str = "",
    run_id: str | None = None,
) -> str:
    target = run_id or STATE.run_id
    if target is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(
            await _client().create_run_control_message(
                target,
                operation=operation,
                causal_position=causal_position,
                expected_run_version=expected_run_version,
                directive=directive,
                reason=reason,
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Launch a child run under the current Run and return its durable handle. "
        "This creates an authoritative Task and delegates work; ask the user before "
        "calling it. Repeating the same correlation_id returns the existing child "
        "instead of spawning a second one."
    ),
    annotations=MUTATING,
)
async def cp_launch_child(
    correlation_id: str,
    title: str,
    description: str = "",
    priority: str = "medium",
    grant: dict[str, Any] | None = None,
    cancellation_policy: str | None = None,
    expires_in_seconds: int | None = None,
    run_id: str | None = None,
) -> str:
    target = run_id or STATE.run_id
    if target is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(
            await _client().launch_child_run(
                target,
                correlation_id=correlation_id,
                title=title,
                description=description,
                priority=priority,
                grant=grant,
                cancellation_policy=cancellation_policy,
                expires_in_seconds=expires_in_seconds,
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "List the child handles launched by a Run. This is how an orchestrator "
        "finds its children again after a restart."
    ),
    annotations=READ_ONLY,
)
async def cp_list_child_handles(
    run_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    active: bool | None = None,
) -> str:
    target = run_id or STATE.run_id
    if target is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(
            await _client().list_child_handles(target, limit=limit, cursor=cursor, active=active)
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Resolve one child handle by id or ch1_ token: derived execution status "
        "plus the bounded terminal result and its hash, if the child finished."
    ),
    annotations=READ_ONLY,
)
async def cp_resolve_child(ref: str) -> str:
    try:
        return _dump(await _client().resolve_child_handle(ref))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Withdraw a child handle, optionally asking the child to stop at its next "
        "safe boundary. This changes authoritative state; ask the user first."
    ),
    annotations=MUTATING,
)
async def cp_revoke_child(handle_id: str, reason: str = "", cancel_child: bool = False) -> str:
    try:
        return _dump(
            await _client().revoke_child_handle(handle_id, reason=reason, cancel_child=cancel_child)
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description="List durable control messages of a Run for restart/recovery.",
    annotations=READ_ONLY,
)
async def cp_list_run_controls(
    run_id: str | None = None, limit: int | None = None, cursor: str | None = None
) -> str:
    target = run_id or STATE.run_id
    if target is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(await _client().list_run_control_messages(target, limit=limit, cursor=cursor))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Acknowledge the oldest accepted Run control message at a safe boundary. "
        "Uses the current Claim and fencing token."
    ),
    annotations=MUTATING,
)
async def cp_ack_run_control(
    message_id: str,
    status: str,
    expected_run_version: int,
    expected_message_version: int,
    safe_boundary: str | None = None,
    reason: str = "",
    run_id: str | None = None,
) -> str:
    target = run_id or STATE.run_id
    if target is None or STATE.claim_id is None or STATE.fencing_token is None:
        return _dump({"error": "no_run", "message": "No claimed run in progress."})
    try:
        return _dump(
            await _client().acknowledge_run_control_message(
                target,
                message_id,
                status=status,
                claim_id=STATE.claim_id,
                fencing_token=STATE.fencing_token,
                expected_run_version=expected_run_version,
                expected_message_version=expected_message_version,
                safe_boundary=safe_boundary,
                reason=reason,
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Persist a durable checkpoint of operational state (branch, next step, "
        "...) so a future session can resume this work. Not for chat history."
    ),
    annotations=MUTATING,
)
async def cp_checkpoint(kind: str, data: dict[str, Any]) -> str:
    if STATE.run_id is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(await _client().create_checkpoint(STATE.run_id, kind=kind, data=data))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Record one executed action in the run's audit trail (tool invocation, "
        "skill use, external effect). References only, never large payloads."
    ),
    annotations=MUTATING,
)
async def cp_record_action(
    action: str,
    status: str = "completed",
    skill: str | None = None,
    external_reference: str | None = None,
) -> str:
    if STATE.run_id is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        return _dump(
            await _client().record_action(
                STATE.run_id,
                action=action,
                status=status,
                skill=skill,
                external_reference=external_reference,
            )
        )
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Finish the current run successfully and complete the task (default). "
        "Only call after the user confirmed the work is done."
    ),
    annotations=MUTATING,
)
async def cp_complete_run(output: dict[str, Any] | None = None, complete_task: bool = True) -> str:
    if STATE.run_id is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        result = await _client().succeed_run(
            STATE.run_id, output=output, complete_task=complete_task
        )
    except ControlPlaneError as exc:
        return _error(exc)
    STATE.run_id = None
    if complete_task:
        await _stop_heartbeats()
        STATE.claim_id = None
        STATE.fencing_token = None
        STATE.task_ref = None
    return _dump(result)


@mcp.tool(
    description="Record an honest failure of the current run (keeps the claim).",
    annotations=MUTATING,
)
async def cp_fail_run(reason: str = "failed") -> str:
    if STATE.run_id is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        run = await _client().fail_run(STATE.run_id, failure_reason=reason)
    except ControlPlaneError as exc:
        return _error(exc)
    STATE.run_id = None
    return _dump(run)


@mcp.tool(
    description=(
        "Suspend the current run while waiting (e.g. for an approval): the run "
        "is archived, the claim is released; continuation is a new claim + run "
        "reading the checkpoints. Checkpoint your state first."
    ),
    annotations=MUTATING,
)
async def cp_suspend_run(
    reason: str = "waiting_approval", waiting_for_approval_id: str | None = None
) -> str:
    if STATE.run_id is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        result = await _client().suspend_run(
            STATE.run_id, reason=reason, waiting_for_approval_id=waiting_for_approval_id
        )
    except ControlPlaneError as exc:
        return _error(exc)
    await _stop_heartbeats()
    STATE.run_id = None
    STATE.claim_id = None
    STATE.fencing_token = None
    STATE.task_ref = None  # cp_remember must not auto-attach to a suspended task
    return _dump(result)


@mcp.tool(
    description=(
        "Prepare a durable human harness handoff: atomically write a handoff checkpoint, "
        "suspend the current Run and release its Claim. Call only after the human explicitly "
        "confirms switching harnesses. Never include secrets, prompts, transcripts or paths."
    ),
    annotations=MUTATING,
)
async def cp_prepare_handoff(
    summary: str,
    next_steps: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    run_id: str | None = None,
) -> str:
    target = run_id or STATE.run_id
    if target is None:
        return _dump({"error": "no_run", "message": "No run in progress."})
    try:
        result = await _client().prepare_handoff(
            target,
            summary=summary,
            next_steps=next_steps,
            evidence_refs=evidence_refs,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    if target == STATE.run_id:
        await _stop_heartbeats()
        STATE.run_id = None
        STATE.claim_id = None
        STATE.fencing_token = None
        STATE.task_ref = None
    return _dump(result)


# --- artifacts ----------------------------------------------------------------


@mcp.tool(
    description=(
        "Register a work product (git commit, PR, file, document, report) as a "
        "Control Plane artifact of the current task and run, in one of three "
        "forms: a reference (uri + metadata), a small JSON 'content', or a local "
        "'file' whose bytes are uploaded to the Control Plane (any media type, "
        "e.g. a document that is a declared output of the task). 'file' excludes "
        "'uri' and 'content'; 'name' defaults to the file name and 'media_type' "
        "is guessed from its extension. file:// URIs are only meaningful in this "
        "environment."
    ),
    annotations=MUTATING,
)
async def cp_create_artifact(
    type: str,
    name: str | None = None,
    uri: str | None = None,
    content: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    supersedes_artifact_id: str | None = None,
    file: str | None = None,
    media_type: str | None = None,
) -> str:
    content_ref: str | None = None
    try:
        client = _client()
        if file is not None:
            if uri is not None or content is not None:
                return _dump(
                    {
                        "error": "invalid_artifact_content",
                        "message": "'file' excludes 'uri' and 'content'.",
                    }
                )
            path = Path(file)
            if not await asyncio.to_thread(path.is_file):
                return _dump({"error": "file_not_found", "message": f"Not a readable file: {file}"})
            guessed, _ = mimetypes.guess_type(path.name)
            upload = await client.upload_artifact_content(
                path, media_type=media_type or guessed or "application/octet-stream"
            )
            content_ref = str(upload["contentRef"])
            name = name or path.name
        if not name:
            return _dump({"error": "invalid_request", "message": "'name' is required."})
        artifact = await client.create_artifact(
            type=type,
            name=name,
            task_ref=STATE.task_ref,
            run_id=STATE.run_id,
            uri=uri,
            content=content,
            content_ref=content_ref,
            metadata=metadata,
            supersedes_artifact_id=supersedes_artifact_id,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(artifact)


@mcp.tool(
    description=(
        "Download the stored content of an artifact — typically an input of the "
        "current task — into a local file and return its path. 'path' is where "
        "to write it (default: a temporary directory outside the working copy). "
        "The artifact is read as an input of 'for_task' (default: the current "
        "task), which works even without access to the task that produced it. "
        "Small text content is also returned inline as 'text'."
    ),
    annotations=READ_ONLY,
)
async def cp_get_artifact_content(
    artifact_id: str, path: str | None = None, for_task: str | None = None
) -> str:
    try:
        client = _client()
        target = Path(path) if path else None
        if target is None:
            record = await client.get_artifact(artifact_id)
            directory = Path(tempfile.gettempdir()) / "control-plane-artifacts" / artifact_id
            target = directory / safe_file_name(str(record.get("name") or ""), artifact_id)
        result = await client.download_artifact_content(
            artifact_id, target, for_task=for_task or STATE.task_ref
        )
    except ControlPlaneError as exc:
        return _error(exc)
    base_type = str(result["mediaType"]).split(";")[0].strip().lower()
    if _TEXT_MEDIA_TYPES.match(base_type) and result["sizeBytes"] <= _INLINE_TEXT_BYTES:
        data = await asyncio.to_thread(target.read_bytes)
        result["text"] = data.decode("utf-8", errors="replace")
    return _dump(result)


@mcp.tool(
    description="List artifacts of a task (defaults to the current task).", annotations=READ_ONLY
)
async def cp_list_artifacts(task_id: str | None = None) -> str:
    try:
        client = _client()
        params: dict[str, Any] = {}
        if task_id:
            params["taskId"] = task_id
        elif STATE.task_ref:
            record = await client.get_task(STATE.task_ref)
            params["taskId"] = record["id"]
        return _dump(await client.list_artifacts(**params))
    except ControlPlaneError as exc:
        return _error(exc)


# --- approvals ----------------------------------------------------------------


@mcp.tool(
    description=(
        "Request an approval. gate=True blocks the task until decided (use "
        "with cp_suspend_run for review gates). Address it to a role id or "
        "a specific principal id (exactly one)."
    ),
    annotations=MUTATING,
)
async def cp_request_approval(
    comment: str = "",
    gate: bool = False,
    required_role_id: str | None = None,
    assigned_principal_id: str | None = None,
    artifact_id: str | None = None,
) -> str:
    try:
        approval = await _client().request_approval(
            task_ref=STATE.task_ref,
            artifact_id=artifact_id,
            required_role_id=required_role_id,
            assigned_principal_id=assigned_principal_id,
            comment=comment,
            gate=gate,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return _dump(approval)


@mcp.tool(
    description="List approvals (default: pending ones addressed to anyone).", annotations=READ_ONLY
)
async def cp_list_approvals(status: str = "pending") -> str:
    try:
        return _dump(await _client().list_approvals(status=status))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description="Approve an approval (requires eligibility; ask the user first).",
    annotations=MUTATING,
)
async def cp_approve(approval_id: str, comment: str | None = None) -> str:
    try:
        return _dump(await _client().approve(approval_id, comment=comment))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description="Reject an approval (requires eligibility; ask the user first).",
    annotations=MUTATING,
)
async def cp_reject(approval_id: str, comment: str | None = None) -> str:
    try:
        return _dump(await _client().reject(approval_id, comment=comment))
    except ControlPlaneError as exc:
        return _error(exc)


# --- events -------------------------------------------------------------------


# --- project model (v0.5) -----------------------------------------------------


@mcp.tool(
    description=(
        "List projects visible to me. A project is a profile on a workspace: "
        "its hierarchy comes from the workspace tree, not from a separate one."
    ),
    annotations=READ_ONLY,
)
async def cp_list_projects(
    workspace_id: str | None = None, status: str | None = None, limit: int = 20
) -> str:
    try:
        page = await _client().list_projects(limit=limit, workspace_id=workspace_id, status=status)
    except ControlPlaneError as exc:
        return _error(exc)
    items = [
        {
            "id": p["id"],
            "workspaceId": p["workspaceId"],
            "parentProjectId": p.get("parentProjectId"),
            "templateKey": p.get("templateKey"),
            "statusKey": p["statusKey"],
            "systemStatusCategory": p["systemStatusCategory"],
            "status": p["status"],
        }
        for p in page["items"]
    ]
    return _dump({"items": items, "nextCursor": page["nextCursor"]})


@mcp.tool(
    description="Inspect one project: profile, derived parent, template and status.",
    annotations=READ_ONLY,
)
async def cp_get_project(project: str) -> str:
    try:
        return _dump(await _client().get_project(project))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Effective configuration of a project with provenance: which layer "
        "(template / ancestor project / revision / profile) produced each key."
    ),
    annotations=READ_ONLY,
)
async def cp_project_config(project: str) -> str:
    try:
        return _dump(await _client().get_effective_config(project))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Focus this session on a project so later cp_list_work / cp_get_context "
        "calls are scoped to it. Local state only: it grants no authority."
    ),
    annotations=MUTATING,
)
async def cp_focus_project(project: str = "") -> str:
    if not project:
        STATE.project_id = None
        return _dump({"projectId": None})
    try:
        body = await _client().get_project(project)
    except ControlPlaneError as exc:
        return _error(exc)
    STATE.project_id = body["id"]
    return _dump(
        {
            "projectId": body["id"],
            "workspaceId": body["workspaceId"],
            "statusKey": body["statusKey"],
            "systemStatusCategory": body["systemStatusCategory"],
        }
    )


@mcp.tool(
    description="Workspace tree with a short project projection on each node.",
    annotations=READ_ONLY,
)
async def cp_workspace_tree(root_id: str | None = None, depth: int | None = None) -> str:
    try:
        return _dump(await _client().get_workspace_tree(root_id=root_id, depth=depth))
    except ControlPlaneError as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Read domain events after an opaque cursor (what happened while I was "
        "away). Use eventCursor from cp_context as the starting point and "
        "the page's nextCursor to continue; never construct cursors yourself."
    ),
    annotations=READ_ONLY,
)
async def cp_list_events(after: str = "", limit: int = 50) -> str:
    try:
        return _dump(await _client().list_events(cursor=after or None, limit=limit))
    except ControlPlaneError as exc:
        return _error(exc)


# --- process authoring (CP-ADR-0074 §17) --------------------------------------

#: Applying a package changes the catalog: never read-only, never idempotent
#: (the same hash a second time is a stale plan), and a plan may retire objects.
APPLYING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
_PACKAGE_SUFFIXES = (".yaml", ".yml")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
#: Journal entries cp_process_explain reads at most; the rest is a cursor.
_EXPLAIN_MAX_ENTRIES = 1000
_PACKAGE_HINTS = {
    "plan_stale": (
        "The catalog or the open instances changed since this plan was built. "
        "Run cp_pkg_plan again, show the new plan to the user and apply its planHash."
    ),
    "migration_required": (
        "Open instances wait on elements the new version drops: add a migration "
        "to the process (migrations: [{from, to, policy, map}]) and plan again."
    ),
    "invalid_package": "Fix the problems (file and line) and plan again.",
}


def _problem(
    code: str, message: str, *, file: str | None = None, hint: str | None = None
) -> dict[str, Any]:
    """A finding of this adapter in the shape of the core's (``ProcessProblemOut``)."""
    return {
        "code": code,
        "severity": "error",
        "path": "",
        "file": file,
        "line": None,
        "message": message,
        "hint": hint,
    }


def _problems_error(code: str, message: str, problems: list[dict[str, Any]]) -> str:
    return _dump({"error": code, "message": message, "problems": problems})


def _package_error(exc: ControlPlaneError) -> str:
    """An error of a package or process tool: the core's findings, or one of this form.

    The core puts the findings of a refused package in ``details.problems``;
    any other refusal (a right, a stale plan, a malformed body) becomes one
    finding of the same shape, so the author reads every answer one way.
    """
    hint = _PACKAGE_HINTS.get(exc.code)
    problems = exc.details.get("problems")
    if not isinstance(problems, list) or not problems:
        errors = exc.details.get("errors")
        if isinstance(errors, list) and errors:
            problems = [
                {
                    **_problem(exc.code, str(item.get("message")), hint=hint),
                    "path": str(item.get("path") or item.get("loc") or ""),
                }
                for item in errors
                if isinstance(item, dict)
            ]
        else:
            problems = [_problem(exc.code, exc.message, hint=hint)]
    payload: dict[str, Any] = {
        "error": exc.code,
        "message": exc.message,
        "details": {k: v for k, v in exc.details.items() if k != "problems"},
        "problems": problems,
    }
    if hint is not None:
        payload["hint"] = hint
    return _dump(payload)


def _read_package(path: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """The YAML files of a package directory as ``{path, content}``, or findings.

    Paths are relative to the directory with ``/``; hidden files and
    directories (``.layout``, ``.git``) are not part of the package.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        return [], [_problem("package_not_found", f"Not a package directory: {path}")]
    files: list[dict[str, str]] = []
    problems: list[dict[str, Any]] = []
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if not item.is_file() or item.suffix not in _PACKAGE_SUFFIXES:
            continue
        name = relative.as_posix()
        try:
            files.append({"path": name, "content": item.read_text(encoding="utf-8")})
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(
                _problem("package_file_unreadable", f"{type(exc).__name__}: {exc}", file=name)
            )
    if not files and not problems:
        problems.append(
            _problem(
                "package_empty",
                f"No YAML files in {path}",
                hint="A package is a directory with package.yaml and its objects",
            )
        )
    return files, problems


async def _package(path: str) -> tuple[list[dict[str, str]], str | None]:
    files, problems = await asyncio.to_thread(_read_package, path)
    if problems:
        return [], _problems_error(problems[0]["code"], problems[0]["message"], problems)
    return files, None


@mcp.tool(
    description=(
        "Check a process package directory in the core without running its "
        "tests: every object against the catalog schema and the process "
        "language. 'problems' name the file, line and JSON pointer of each "
        "finding (code, severity, message, hint). Writes nothing."
    ),
    annotations=READ_ONLY,
)
async def cp_pkg_check(path: str, workspace_id: str = "") -> str:
    files, refused = await _package(path)
    if refused is not None:
        return refused
    try:
        report = await _client().test_package(
            files, workspace_id=workspace_id or None, check_only=True
        )
    except ControlPlaneError as exc:
        return _package_error(exc)
    return _dump({"status": report["status"], "problems": report["problems"]})


@mcp.tool(
    description=(
        "Check a process package directory and run its tests (tests/*.test.yaml) "
        "in the core's sandbox: status passed | failed | invalid, problems, "
        "failures by test step, coverage by process with what no test reached. "
        "'tests' narrows the run to these test files. Writes nothing."
    ),
    annotations=READ_ONLY,
)
async def cp_pkg_test(path: str, tests: list[str] | None = None, workspace_id: str = "") -> str:
    files, refused = await _package(path)
    if refused is not None:
        return refused
    try:
        return _dump(
            await _client().test_package(files, tests=tests, workspace_id=workspace_id or None)
        )
    except ControlPlaneError as exc:
        return _package_error(exc)


@mcp.tool(
    description=(
        "Plan applying a process package directory: what changes in the catalog "
        "by object and field (and who owns each field), the behavioural diff of "
        "each changed process replayed on real instances, the fate of open "
        "instances (pin / migrate), regulation coverage, problems — and the "
        "planHash. Show the plan to the user; cp_pkg_apply applies exactly this "
        "hash. Writes nothing."
    ),
    annotations=READ_ONLY,
)
async def cp_pkg_plan(
    path: str,
    workspace_id: str = "",
    replay_limit: int | None = None,
    overwrite_console: bool = False,
) -> str:
    files, refused = await _package(path)
    if refused is not None:
        return refused
    try:
        return _dump(
            await _client().plan_package(
                files,
                workspace_id=workspace_id or None,
                replay_limit=replay_limit,
                overwrite_console=overwrite_console,
            )
        )
    except ControlPlaneError as exc:
        return _package_error(exc)


@mcp.tool(
    description=(
        "Apply a process package directory — only the plan the user saw: "
        "'plan_hash' is the planHash of cp_pkg_plan, required. The core builds "
        "the plan again and refuses plan_stale when the catalog or the open "
        "instances changed since; then plan again. Pass the workspace_id and "
        "overwrite_console the plan was built with. Call only after the user "
        "approved the plan."
    ),
    annotations=APPLYING,
)
async def cp_pkg_apply(
    path: str, plan_hash: str, workspace_id: str = "", overwrite_console: bool = False
) -> str:
    if not _PLAN_HASH.match(plan_hash or ""):
        problem = _problem(
            "plan_hash_required",
            "A package is applied only by the hash of its plan (sha256:<64 hex>)",
            hint="Run cp_pkg_plan, show the plan to the user and pass its planHash",
        )
        return _problems_error(problem["code"], problem["message"], [problem])
    files, refused = await _package(path)
    if refused is not None:
        return refused
    try:
        return _dump(
            await _client().apply_package(
                files,
                plan_hash=plan_hash,
                workspace_id=workspace_id or None,
                overwrite_console=overwrite_console,
            )
        )
    except ControlPlaneError as exc:
        return _package_error(exc)


@mcp.tool(
    description=(
        "A published process by key (latest version) or key@version: its spec, "
        "definition hash, identity agent, owner and warnings, plus its versions, "
        "newest first."
    ),
    annotations=READ_ONLY,
)
async def cp_process_get(ref: str, versions_limit: int = 20) -> str:
    try:
        client = _client()
        definition = await client.get_process_definition(ref)
        versions = await client.list_process_versions(definition["key"], limit=versions_limit)
    except ControlPlaneError as exc:
        return _package_error(exc)
    return _dump(
        {
            "definition": definition,
            "versions": {"items": versions["items"], "nextCursor": versions["nextCursor"]},
        }
    )


def _regulations(spec: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[Any]]]:
    """``governedBy`` of the process, and of each element with what encloses it.

    An element answers to its own documents and to those of the stages and
    the process around it, nearest first; each item says where it is declared
    (``element``, ``null`` for the process). The data schema is not walked.
    """

    def items(node: Mapping[str, Any], element: str | None) -> list[dict[str, Any]]:
        return [
            {**item, "element": element}
            for item in node.get("governedBy") or ()
            if isinstance(item, Mapping)
        ]

    process = items(spec, None)
    by_element: dict[str, list[Any]] = {}

    def walk(node: Any, inherited: list[dict[str, Any]]) -> None:
        if isinstance(node, Mapping):
            element = node.get("id")
            if isinstance(element, str):
                inherited = items(node, element) + inherited
                by_element.setdefault(element, inherited)
            for key, value in node.items():
                if key != "governedBy":
                    walk(value, inherited)
        elif isinstance(node, list):
            for value in node:
                walk(value, inherited)

    walk({k: v for k, v in spec.items() if k not in ("governedBy", "data")}, process)
    return process, by_element


@mcp.tool(
    description=(
        "Explain a process instance: its state and, step by step from the "
        "decision journal, what came in (and from whom), what the engine "
        "decided and why, what it did, what memory answered to its recall "
        "steps, and which regulations (governedBy) govern each decision — read "
        "against the version the instance runs on."
    ),
    annotations=READ_ONLY,
)
async def cp_process_explain(instance_id: str) -> str:
    try:
        client = _client()
        instance = await client.get_process_instance(instance_id)
        definition = await client.get_process_definition(
            f"{instance['definitionKey']}@{instance['definitionVersion']}"
        )
        entries: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await client.list_process_journal(instance_id, limit=200, cursor=cursor)
            entries.extend(page["items"])
            cursor = page["nextCursor"]
            if cursor is None or len(entries) >= _EXPLAIN_MAX_ENTRIES:
                break
    except ControlPlaneError as exc:
        return _package_error(exc)
    process_rules, element_rules = _regulations(definition["spec"])

    def governed(element: str | None) -> list[Any]:
        if element is None:
            return process_rules
        # A timer or a branch of a step is named after it: "<step>:<part>".
        return element_rules.get(element) or element_rules.get(element.split(":")[0], process_rules)

    steps: list[dict[str, Any]] = []
    for entry in entries:
        if entry["kind"] == "input":
            received = entry["data"].get("input") or {}
            steps.append(
                {
                    "seq": entry["seq"],
                    "at": entry["at"],
                    "input": entry["reason"],
                    "inputKind": received.get("kind"),
                    "body": received.get("body"),
                    "actorId": entry["actorId"],
                    "eventId": entry["eventId"],
                    "decisions": [],
                    "intents": [],
                }
            )
            continue
        if not steps or steps[-1]["seq"] != entry["seq"]:  # pragma: no cover - input first
            continue
        explained = {
            "kind": entry["kind"],
            "element": entry["element"],
            "reason": entry["reason"],
            "data": entry["data"],
            "governedBy": governed(entry["element"]),
        }
        steps[-1]["intents" if entry["kind"] == "intent" else "decisions"].append(explained)
    # What memory answered: the recall inputs, by the step that asked.
    memory = [
        {
            "seq": step["seq"],
            "at": step["at"],
            "element": next(
                (d["element"] for d in step["decisions"] if d["kind"] == "recall"), None
            ),
            **(step["body"] or {}),
        }
        for step in steps
        if step["inputKind"] == "recall"
    ]
    return _dump(
        {
            "instance": instance,
            "process": {
                "key": definition["key"],
                "version": definition["version"],
                "latestVersion": definition["latestVersion"],
                "displayName": definition["displayName"],
                "definitionHash": definition["definitionHash"],
                "governedBy": process_rules,
            },
            "steps": steps,
            "memory": memory,
            "journalCursor": cursor,
        }
    )


def main() -> None:  # pragma: no cover - process entrypoint
    adopt_run_from_environment()
    mcp.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
