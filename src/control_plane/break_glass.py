"""Break-glass CLI (ADR-0065): run inside the control-plane-api container.

    docker compose exec control-plane-api python -m control_plane.break_glass \\
        issue --principal <uuid> --ttl 3600 --reason "IAM down, restoring access"
    docker compose exec control-plane-api python -m control_plane.break_glass revoke

``issue`` prints the key once, to stdout; nothing else stores it. ``revoke``
revokes every live break-glass key — run it as soon as IAM is back.
"""

import argparse
import asyncio
import getpass
import os
import socket
import sys
import uuid

from control_plane.application.commands.break_glass import (
    issue_break_glass_key,
    revoke_break_glass_keys,
)
from control_plane.config import Settings, get_settings
from control_plane.domain.errors import DomainError
from control_plane.infrastructure.db.engine import (
    build_engine,
    build_session_factory,
    transaction,
)


def _issued_by() -> str:
    # Inside a container the login is rarely meaningful; the host operator can
    # state who they are, and the container hostname still pins the place.
    who = os.environ.get("BREAK_GLASS_OPERATOR") or getpass.getuser()
    return f"{who}@{socket.gethostname()}"


async def _issue(settings: Settings, args: argparse.Namespace) -> int:
    engine = build_engine(settings)
    try:
        async with transaction(build_session_factory(engine)) as session:
            issued = await issue_break_glass_key(
                session,
                settings,
                principal_id=args.principal,
                ttl_seconds=args.ttl,
                reason=args.reason,
                issued_by=_issued_by(),
            )
    finally:
        await engine.dispose()
    print(
        f"break-glass key for principal {args.principal}, "
        f"expires {issued.api_key.expires_at} "
        f"(prefix {issued.api_key.key_prefix}). Shown once:",
        file=sys.stderr,
    )
    print(issued.generated.full_key)
    return 0


async def _revoke(settings: Settings) -> int:
    engine = build_engine(settings)
    try:
        async with transaction(build_session_factory(engine)) as session:
            revoked = await revoke_break_glass_keys(session, issued_by=_issued_by())
    finally:
        await engine.dispose()
    print(f"revoked break-glass keys: {len(revoked)}", file=sys.stderr)
    for api_key in revoked:
        print(f"  {api_key.key_prefix}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m control_plane.break_glass",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="mint a short-lived admin key for a human")
    issue.add_argument(
        "--principal", type=uuid.UUID, required=True, help="Control Plane principal id"
    )
    issue.add_argument(
        "--ttl", type=int, default=3600, help="seconds, 60..CP_BREAK_GLASS_MAX_TTL_SECONDS"
    )
    issue.add_argument("--reason", required=True, help="why; lands in the event journal")
    commands.add_parser("revoke", help="revoke every live break-glass key")
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        if args.command == "issue":
            return asyncio.run(_issue(settings, args))
        return asyncio.run(_revoke(settings))
    except DomainError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
