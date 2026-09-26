"""``control-plane`` — minimal CLI over the official client SDK.

Debugging / smoke-testing / Claude Code fallback, deliberately not a UI
product. Server resolution order: --server flag, CONTROL_PLANE_SERVER env,
.control-plane/config.json up the directory tree. Credentials: see
control_plane_client.credentials (never flags — keys must not enter shell
history).

v0.5 removed the codename-era aliases, env vars and config locations
(ADR-0040); a stale legacy setup gets an explicit migration error instead of
a confusing "not configured".
"""

import argparse
import asyncio
import getpass
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

from control_plane_client import (
    ControlPlaneClient,
    ControlPlaneError,
    delete_api_key,
    find_project_config,
    store_api_key,
    write_project_config,
)
from control_plane_client.credentials import (
    removed_environment_variables,
    resolve_credential,
)

PROG = "control-plane"


def _warn_removed_environment() -> None:
    """Fail loudly when only a removed codename-era variable is set."""
    stale = removed_environment_variables()
    if not stale:
        return
    for name, replacement in sorted(stale.items()):
        print(
            f"{PROG}: {name} is no longer read (removed in v0.5); use {replacement}",
            file=sys.stderr,
        )
    raise SystemExit(2)


def _resolve_server(args: argparse.Namespace) -> str:
    _warn_removed_environment()
    if getattr(args, "server", None):
        return str(args.server).rstrip("/")
    env = os.environ.get("CONTROL_PLANE_SERVER")
    if env:
        return env.rstrip("/")
    config = find_project_config()
    if config is not None:
        return config.server
    print(
        f"{PROG}: no server configured (use --server, CONTROL_PLANE_SERVER, or "
        f"`{PROG} init` to create .control-plane/config.json)",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _client(args: argparse.Namespace) -> ControlPlaneClient:
    server = _resolve_server(args)
    # IAM identity first (CONTROL_PLANE_IAM_URL plus a Platform Access Token from
    # credentials.json or the environment); the legacy API key only when IAM is not
    # configured -- the same order the MCP server uses.
    credential = resolve_credential(server)
    if credential is None:
        print(
            f"{PROG}: no credentials for {server} (configure CONTROL_PLANE_IAM_URL and a "
            f"Platform Access Token, or run `{PROG} login --server {server}` for a legacy key)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return ControlPlaneClient(server, credential)


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


# --- commands -----------------------------------------------------------------


async def cmd_whoami(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        context = await client.get_context()
    _print(
        {
            "tenant": context["tenant"],
            "principal": context["principal"],
            "permissions": context["permissions"],
        }
    )


async def cmd_context(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.get_context(session_id=args.session))


async def cmd_work_list(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        page = await client.list_available_work(
            limit=args.limit,
            workspace_id=args.workspace,
            include_descendants=args.include_descendants,
            project_id=args.project,
            include_subprojects=args.include_subprojects,
        )
    for task in page["items"]:
        print(f"{task['publicId']}  [{task['priority']:>8}]  {task['title']}")
    if not page["items"]:
        print("(no available work)")


async def cmd_task_get(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.get_task(args.task))


async def cmd_task_claimability(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.get_claimability(args.task))


async def cmd_task_claim(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        session = await client.open_session(
            client_name="control-plane-cli", harness_type="cli", capabilities=["resume"]
        )
        claim = await client.claim_task(args.task, session["id"], intent=args.intent)
    _print({"sessionId": session["id"], "claim": claim})


async def cmd_run_start(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        run = await client.start_run(
            args.task, claim_id=args.claim, fencing_token=args.fencing_token
        )
    _print(run)


async def cmd_run_status(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.get_run(args.run))


async def cmd_artifact_add(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        artifact = await client.create_artifact(
            type=args.type, name=args.name, task_ref=args.task, run_id=args.run, uri=args.uri
        )
    _print(artifact)


async def cmd_approvals_list(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        page = await client.list_approvals(status=args.status)
    _print(page["items"])


async def cmd_events_tail(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        cursor = args.after
        if cursor is None:
            if args.replay:
                page = await client.list_events(tail=args.replay)
                for event in page["items"]:
                    _print_event(event)
                cursor = page["nextCursor"]
            else:
                cursor = (await client.get_context())["eventCursor"]
        try:
            async for event in client.follow_events(cursor=cursor):
                _print_event(event)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            pass


def _print_event(event: dict[str, Any]) -> None:
    print(
        f"{event['sequence']:>8}  {event['occurredAt']}  "
        f"{event['type']:<28} {event['entityType']}:{event['entityId']}"
    )


async def cmd_login(args: argparse.Namespace) -> None:
    server = _resolve_server(args)
    api_key = os.environ.get("CONTROL_PLANE_API_KEY") or getpass.getpass(f"API key for {server}: ")
    async with ControlPlaneClient(server, api_key) as client:
        context = await client.get_context()  # validates the key
    location = store_api_key(server, api_key)
    principal = context["principal"]["displayName"]
    print(f"Authenticated as {principal!r}; credential stored in {location}.")


async def cmd_logout(args: argparse.Namespace) -> None:
    server = _resolve_server(args)
    delete_api_key(server)
    print(f"Credential for {server} removed (server-side revocation is separate).")


async def cmd_project_list(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        page = await client.list_projects(
            limit=args.limit, workspace_id=args.workspace, status=args.status
        )
    for project in page["items"]:
        print(
            f"{project['id']}  [{project['systemStatusCategory']:>17}]  "
            f"{project['statusKey']}  {project['templateKey']}"
        )
    if not page["items"]:
        print("(no projects)")


async def cmd_project_get(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.get_project(args.project))


async def cmd_project_config(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.get_effective_config(args.project))


async def cmd_ops_adapter_status(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.adapter_status())


async def cmd_ops_adapter_redrive(args: argparse.Namespace) -> None:
    async with _client(args) as client:
        _print(await client.redrive_adapter(args.tenant, reason=args.reason))


async def cmd_init(args: argparse.Namespace) -> None:
    server = args.server or os.environ.get("CONTROL_PLANE_SERVER")
    if not server:
        print(f"{PROG} init requires --server", file=sys.stderr)
        raise SystemExit(2)
    path = write_project_config(
        Path.cwd(),
        server=server,
        workspace=args.workspace,
        project=args.project,
        repository=args.repository,
    )
    print(f"Wrote {path} (non-secret metadata only; credentials via `{PROG} login`).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Control Plane CLI")
    try:
        version = importlib.metadata.version("control-plane")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - source-only fallback
        version = "unknown"
    parser.add_argument("--version", action="version", version=f"%(prog)s {version}")
    parser.add_argument("--server", help="Control Plane server URL (overrides env/config)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="identity and permissions").set_defaults(func=cmd_whoami)
    context_p = sub.add_parser("context", help="full harness bootstrap context")
    context_p.add_argument("--session", help="scope to a session id")
    context_p.set_defaults(func=cmd_context)

    work = sub.add_parser("work", help="work discovery").add_subparsers(dest="sub", required=True)
    lp = work.add_parser("list", help="available work for this principal")
    lp.add_argument("--limit", type=int, default=None)
    lp.add_argument("--workspace", help="workspace id filter")
    lp.add_argument("--include-descendants", action="store_true")
    lp.add_argument("--project", help="project id filter (exact scope by default)")
    lp.add_argument("--include-subprojects", action="store_true")
    lp.set_defaults(func=cmd_work_list)

    task = sub.add_parser("task", help="task operations").add_subparsers(dest="sub", required=True)
    gp = task.add_parser("get")
    gp.add_argument("task")
    gp.set_defaults(func=cmd_task_get)
    cp = task.add_parser("claimability")
    cp.add_argument("task")
    cp.set_defaults(func=cmd_task_claimability)
    clp = task.add_parser("claim")
    clp.add_argument("task")
    clp.add_argument("--intent", default="")
    clp.set_defaults(func=cmd_task_claim)

    run = sub.add_parser("run", help="run operations").add_subparsers(dest="sub", required=True)
    sp = run.add_parser("start")
    sp.add_argument("task")
    sp.add_argument("--claim", required=True)
    sp.add_argument("--fencing-token", type=int, required=True)
    sp.set_defaults(func=cmd_run_start)
    stp = run.add_parser("status")
    stp.add_argument("run")
    stp.set_defaults(func=cmd_run_status)

    artifact = sub.add_parser("artifact", help="artifacts").add_subparsers(
        dest="sub", required=True
    )
    ap = artifact.add_parser("add")
    ap.add_argument("--type", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--task")
    ap.add_argument("--run")
    ap.add_argument("--uri")
    ap.set_defaults(func=cmd_artifact_add)

    approvals = sub.add_parser("approvals", help="approvals").add_subparsers(
        dest="sub", required=True
    )
    alp = approvals.add_parser("list")
    alp.add_argument("--status", default="pending")
    alp.set_defaults(func=cmd_approvals_list)

    events = sub.add_parser("events", help="event journal").add_subparsers(
        dest="sub", required=True
    )
    ep = events.add_parser("tail")
    ep.add_argument("--after", default=None, help="opaque event cursor to resume from")
    ep.add_argument("--replay", type=int, default=10, help="replay N recent events first")
    ep.set_defaults(func=cmd_events_tail)

    project = sub.add_parser("project", help="project model").add_subparsers(
        dest="sub", required=True
    )
    plp = project.add_parser("list")
    plp.add_argument("--limit", type=int, default=None)
    plp.add_argument("--workspace")
    plp.add_argument("--status")
    plp.set_defaults(func=cmd_project_list)
    pgp = project.add_parser("get")
    pgp.add_argument("project")
    pgp.set_defaults(func=cmd_project_get)
    pcp = project.add_parser("config", help="effective config with provenance")
    pcp.add_argument("project")
    pcp.set_defaults(func=cmd_project_config)

    ops = sub.add_parser("ops", help="operator actions").add_subparsers(dest="sub", required=True)
    adapter = ops.add_parser("adapter").add_subparsers(dest="action", required=True)
    adapter.add_parser("status").set_defaults(func=cmd_ops_adapter_status)
    arp = adapter.add_parser("redrive", help="un-park delivery and retry the same position")
    arp.add_argument("tenant", help="tenant id (must be your own)")
    arp.add_argument("--reason", default="operator_redrive")
    arp.set_defaults(func=cmd_ops_adapter_redrive)

    sub.add_parser("login", help="store an API key in the local credential store").set_defaults(
        func=cmd_login
    )
    sub.add_parser("logout", help="remove the stored API key").set_defaults(func=cmd_logout)

    init_p = sub.add_parser("init", help="write .control-plane/config.json for this project")
    init_p.add_argument("--server", help="Control Plane server URL for this project")
    init_p.add_argument("--workspace")
    init_p.add_argument("--project")
    init_p.add_argument("--repository")
    init_p.set_defaults(func=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except ControlPlaneError as exc:
        print(f"{PROG}: {exc.code}: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
