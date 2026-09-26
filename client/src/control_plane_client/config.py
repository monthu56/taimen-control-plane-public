"""Local project binding: ``.control-plane/config.json`` (non-secret metadata).

Links a working directory/repository to a Control Plane server + workspace.
Secrets never belong here — see credentials.py. Recommended .gitignore entry:

    .control-plane/*.local.json

(config.json itself is intentionally committable: it holds no secrets.)

v0.5 removed the codename-era ``.taimen/`` fallback (ADR-0040); only the
neutral directory is read.
"""

import json
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = ".control-plane"
LEGACY_CONFIG_DIR = ".taimen"  # removed in v0.5; kept only for the diagnostic
CONFIG_FILE = "config.json"


class LegacyProjectConfigError(RuntimeError):
    """A codename-era ``.taimen/config.json`` was found and is not read."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path} is a removed v0.4 project binding. Move it to "
            f"{CONFIG_DIR}/{CONFIG_FILE} (see docs/migration-v0.5.md)."
        )
        self.path = path


@dataclass(frozen=True)
class ProjectConfig:
    server: str
    tenant: str | None = None
    workspace: str | None = None
    # v0.5: the Project this working directory belongs to (id or workspace
    # slug). Non-secret metadata, like everything else in this file.
    project: str | None = None
    repository: str | None = None
    path: Path | None = None


def _load(candidate: Path) -> ProjectConfig | None:
    try:
        data = json.loads(candidate.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("server"):
        return None
    return ProjectConfig(
        server=str(data["server"]).rstrip("/"),
        tenant=data.get("tenant"),
        workspace=data.get("workspace"),
        project=data.get("project"),
        repository=data.get("repository"),
        path=candidate,
    )


def find_project_config(start: Path | None = None) -> ProjectConfig | None:
    """Walk up from `start` (cwd) looking for the project config.

    The search stops at the enclosing repository root (a directory holding
    ``.git``) and never leaves the user's home directory: an unrelated config
    in some ancestor must not silently decide which server this project talks
    to — the server URL is where credentials get sent.
    """
    current = (start or Path.cwd()).resolve()
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - exotic environments
        home = None
    for directory in [current, *current.parents]:
        candidate = directory / CONFIG_DIR / CONFIG_FILE
        if candidate.is_file():
            return _load(candidate)
        if (directory / LEGACY_CONFIG_DIR / CONFIG_FILE).is_file():
            # Explicit over silent: the codename-era binding is no longer read
            # (ADR-0040), and pretending it does not exist would be confusing.
            raise LegacyProjectConfigError(directory / LEGACY_CONFIG_DIR / CONFIG_FILE)
        # Boundaries are checked only after this directory's own config, so a
        # repo root that carries the config is still honored.
        if (directory / ".git").exists() or (home is not None and directory == home):
            return None
    return None


def write_project_config(
    directory: Path,
    *,
    server: str,
    tenant: str | None = None,
    workspace: str | None = None,
    project: str | None = None,
    repository: str | None = None,
) -> Path:
    target = directory / CONFIG_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / CONFIG_FILE
    payload = {"server": server.rstrip("/")}
    if tenant:
        payload["tenant"] = tenant
    if workspace:
        payload["workspace"] = workspace
    if project:
        payload["project"] = project
    if repository:
        payload["repository"] = repository
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path
