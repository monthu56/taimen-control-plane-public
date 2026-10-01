#!/usr/bin/env python3
"""Replay every process instance of a tenant on its own version: the replay gate of a rollout.

process-observability P018 (SC-009; used by the staging rollout, P025). For
every process the key can read, every version its instances run on is fed
back its own journals through ``POST /process-definitions/{key}:replay``
(CP-ADR-0074 §10) — the spec of that very version as the candidate, the
instances by id, in batches. The core compares the decisions, the intents and
the final state with what it stored; nothing is written. The report lists
every divergence, every problem of a version and every call that failed.

Exit code: 0 — zero divergences and every instance replayed; 1 — a
divergence, a version whose check reports an error, or an instance left out;
2 — the tenant could not be walked (a call failed, a process asked for by
``--process`` is not there, no credential).

The credential is the one the canonical client resolves
(``control_plane_client.credentials.resolve_credential``), never an argument:
an IAM access token exchanged for the Platform Access Token of ``iam auth
login`` (``CONTROL_PLANE_IAM_URL``, ``CONTROL_PLANE_IAM_TENANT``; it lives
minutes and is exchanged again when it expires), else an API key
(``CONTROL_PLANE_API_KEY``, the keychain, the credentials file) — on an
IAM-only installation that is the break-glass key (CP-ADR-0065). The
principal needs ``processes.read`` and ``packages.test``. Usage::

    CONTROL_PLANE_IAM_URL=<IAM> CONTROL_PLANE_IAM_TENANT=<tenant> \\
        uv run python scripts/process_replay_all.py \\
        --base-url <core> --out replay-report.json

The report holds instance ids and keys and, for a divergence, what was
recorded and what the code decides — a fragment of the journal. It holds no
credentials.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from control_plane_client.credentials import CredentialProvider, resolve_credential
from control_plane_client.errors import ControlPlaneError

API = "/api/v1"
# The most instances one call replays (REPLAY_MAX_INSTANCES of the API).
BATCH = 200
PAGE = 100


class WalkError(Exception):
    """A call the walk needs failed: the report cannot be complete."""

    def __init__(self, what: str, response: httpx.Response) -> None:
        super().__init__(f"{what}: HTTP {response.status_code}")
        self.what = what
        self.status = response.status_code
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        self.code = error.get("code") if isinstance(error, dict) else None
        self.message = error.get("message") if isinstance(error, dict) else None

    def out(self) -> dict[str, Any]:
        return {
            "call": self.what,
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }


@dataclass
class VersionReport:
    """One version of a process replayed on the journals of its instances."""

    key: str
    version: int
    instances: int
    replayed: int = 0
    diverged: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return any(p.get("severity") == "error" for p in self.problems)

    @property
    def ok(self) -> bool:
        return (
            not self.diverged
            and not self.errors
            and not self.refused
            and self.replayed == self.instances
        )

    def out(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "instances": self.instances,
            "replayed": self.replayed,
            "diverged": len(self.diverged),
            "ok": self.ok,
            "divergences": self.diverged,
            "problems": self.problems,
            "errors": self.errors,
        }


@dataclass
class Report:
    base_url: str
    started_at: str
    finished_at: str | None = None
    versions: list[VersionReport] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(v.ok for v in self.versions)

    def totals(self) -> dict[str, int]:
        return {
            "processes": len({v.key for v in self.versions}),
            "versions": len(self.versions),
            "instances": sum(v.instances for v in self.versions),
            "replayed": sum(v.replayed for v in self.versions),
            "diverged": sum(len(v.diverged) for v in self.versions),
            "refusedVersions": sum(1 for v in self.versions if v.refused),
            "errors": len(self.errors) + sum(len(v.errors) for v in self.versions),
        }

    def out(self) -> dict[str, Any]:
        return {
            "baseUrl": self.base_url,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "ok": self.ok,
            "totals": self.totals(),
            "versions": [v.out() for v in self.versions],
            "errors": self.errors,
        }


class CredentialAuth(httpx.Auth):
    """A Bearer from the client's credential: taken per call, renewed once on a 401.

    An IAM access token expires within minutes, a walk of a tenant may take
    longer; a second 401 is a real denial and goes into the report.
    """

    def __init__(self, credential: CredentialProvider) -> None:
        self.credential = credential

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self.credential.token()}"
        response = yield request
        if response.status_code == 401 and self.credential.refreshable:
            await self.credential.refresh()
            request.headers["Authorization"] = f"Bearer {await self.credential.token()}"
            yield request


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def _get(
    client: httpx.AsyncClient, path: str, headers: Mapping[str, str], **params: Any
) -> dict[str, Any]:
    response = await client.get(f"{API}{path}", params=params, headers=dict(headers))
    if response.status_code != 200:
        raise WalkError(f"GET {path}", response)
    body: dict[str, Any] = response.json()
    return body


async def _pages(
    client: httpx.AsyncClient, path: str, headers: Mapping[str, str], **params: Any
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        page = await _get(client, path, headers, limit=PAGE, **params, **extra)
        items += page["items"]
        cursor = page.get("nextCursor")
        if not cursor:
            return items


async def _processes(
    client: httpx.AsyncClient, headers: Mapping[str, str], only: Sequence[str] | None
) -> list[str]:
    if only:
        return sorted(dict.fromkeys(only))
    return sorted({item["key"] for item in await _pages(client, "/process-definitions", headers)})


async def _known(client: httpx.AsyncClient, headers: Mapping[str, str], key: str) -> None:
    """A process asked for by key is there: one the key cannot see is not an empty one."""
    await _get(client, f"/process-definitions/{quote(key, safe='')}", headers)


async def _replay_version(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    key: str,
    version: int,
    instance_ids: list[str],
    batch: int,
) -> VersionReport:
    report = VersionReport(key, version, len(instance_ids))
    ref = quote(f"{key}@{version}", safe="@")
    try:
        spec = (await _get(client, f"/process-definitions/{ref}", headers))["spec"]
    except WalkError as exc:
        report.errors.append(exc.out())
        return report
    for start in range(0, len(instance_ids), batch):
        chunk = instance_ids[start : start + batch]
        response = await client.post(
            f"{API}/process-definitions/{quote(key, safe='')}:replay",
            json={"spec": spec, "instanceIds": chunk, "limit": len(chunk)},
            headers=dict(headers),
        )
        if response.status_code != 200:
            report.errors.append(WalkError(f"POST {key}:replay", response).out())
            continue
        body = response.json()
        for problem in body.get("problems") or ():
            if problem not in report.problems:
                report.problems.append(problem)
        for item in body.get("instances") or ():
            report.replayed += 1
            if item.get("divergences"):
                report.diverged.append(
                    {
                        "instanceId": item["instanceId"],
                        "instanceKey": item["instanceKey"],
                        "events": item["events"],
                        "divergences": item["divergences"],
                    }
                )
    return report


async def replay_tenant(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    *,
    processes: Sequence[str] | None = None,
    batch: int = BATCH,
) -> Report:
    """Replay every instance the key can read, version by version, on its own version.

    ``client`` has the core's base URL; ``headers`` authenticate. A call that
    fails is recorded in the report, never raised: the report says what could
    not be checked.
    """
    if not 1 <= batch <= BATCH:
        raise ValueError(f"batch must be within 1..{BATCH}")
    report = Report(str(client.base_url), _now())
    try:
        keys = await _processes(client, headers, processes)
    except WalkError as exc:
        report.errors.append(exc.out())
        report.finished_at = _now()
        return report
    for key in keys:
        try:
            if processes:
                await _known(client, headers, key)
            instances = await _pages(client, "/process-instances", headers, definitionKey=key)
        except WalkError as exc:
            report.errors.append({**exc.out(), "process": key})
            continue
        by_version: dict[int, list[str]] = {}
        for instance in instances:
            by_version.setdefault(int(instance["definitionVersion"]), []).append(instance["id"])
        for version in sorted(by_version):
            report.versions.append(
                await _replay_version(client, headers, key, version, by_version[version], batch)
            )
    report.finished_at = _now()
    return report


def exit_code(report: Report) -> int:
    if report.errors or any(v.errors for v in report.versions):
        return 2
    return 0 if report.ok else 1


def summary(report: Report) -> str:
    totals = report.totals()
    lines = [
        f"replay of {report.base_url}: {totals['processes']} processes,"
        f" {totals['versions']} versions, {totals['replayed']}/{totals['instances']} instances"
        f" replayed, {totals['diverged']} diverged, {totals['refusedVersions']} versions refused,"
        f" {totals['errors']} errors — {'OK' if report.ok else 'FAILED'}"
    ]
    for version in report.versions:
        if version.ok:
            continue
        lines.append(
            f"  {version.key}@{version.version}: {version.replayed}/{version.instances} replayed,"
            f" {len(version.diverged)} diverged"
        )
        for item in version.diverged:
            [first] = item["divergences"][:1]
            lines.append(
                f"    {item['instanceKey']} ({item['instanceId']}): {first['kind']}"
                f" at journal entry {first['journalSeq']}, element {first['element']}"
            )
        for problem in version.problems:
            if problem.get("severity") == "error":
                lines.append(f"    problem {problem.get('code')}: {problem.get('message')}")
        for error in version.errors:
            lines.append(f"    error {error['call']}: {error['status']} {error.get('code')}")
    for error in report.errors:
        lines.append(f"  error {error['call']}: {error['status']} {error.get('code')}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CONTROL_PLANE_SERVER", "http://127.0.0.1:8000"),
        help="The core, without /api/v1 (the staging address: tests/fixtures/process_journals)",
    )
    parser.add_argument("--process", action="append", help="Only this process key (repeatable)")
    parser.add_argument("--batch", type=int, default=BATCH, help=f"Instances per call, ≤ {BATCH}")
    parser.add_argument("--out", help="Write the JSON report to this file")
    parser.add_argument("--timeout", type=float, default=120.0, help="Seconds per call")
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")
    try:
        credential = resolve_credential(base_url)
    except ControlPlaneError as exc:
        print(f"no credential: {exc}", file=sys.stderr)
        return 2
    if credential is None:
        print(
            "no credential: set CONTROL_PLANE_IAM_URL and CONTROL_PLANE_IAM_TENANT after"
            " `iam auth login`, or CONTROL_PLANE_API_KEY (the break-glass key)",
            file=sys.stderr,
        )
        return 2

    async def run() -> Report:
        # Exchanged once up front: an IAM refusal is one message, not an error per call.
        await credential.token()
        async with httpx.AsyncClient(
            base_url=base_url, timeout=args.timeout, auth=CredentialAuth(credential)
        ) as client:
            return await replay_tenant(client, {}, processes=args.process, batch=args.batch)

    try:
        report = asyncio.run(run())
    except ControlPlaneError as exc:
        print(f"credential refused: {exc}", file=sys.stderr)
        return 2
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report.out(), handle, ensure_ascii=False, indent=1)
            handle.write("\n")
    print(summary(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
