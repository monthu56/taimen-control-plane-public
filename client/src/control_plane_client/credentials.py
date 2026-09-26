"""Local harness credential resolution (ADR-0017).

Credentials NEVER live in the repository, CLAUDE.md, MCP config or shell
history. Resolution order:

1. ``CONTROL_PLANE_API_KEY`` environment variable (explicit override,
   CI-friendly);
2. macOS Keychain (``security`` CLI), when available;
3. ``~/.config/control-plane/credentials.json`` (chmod 0600), keyed by
   server URL.

``login`` writes to the most secure available store; ``logout`` removes the
entry. Server-side revocation is the API-key revoke endpoint.

v0.5 closed the codename compatibility window (ADR-0040): the legacy env var,
keychain service and config directory are no longer read. A setup that still
has only the old location gets an explicit migration error from
:func:`legacy_configuration_error` rather than a silent "no credentials".
"""

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Protocol

from control_plane_client.iam import iam_credential_from_environment

_ENV_VAR = "CONTROL_PLANE_API_KEY"
_KEYCHAIN_SERVICE = "control-plane.api-key"

# Removed in v0.5. Kept ONLY to produce an actionable error; never read as a
# credential source.
_REMOVED_ENV_VARS = {
    "TAIMEN_API_KEY": "CONTROL_PLANE_API_KEY",
    "TAIMEN_SERVER": "CONTROL_PLANE_SERVER",
    "TAIMEN_NO_KEYCHAIN": "CONTROL_PLANE_NO_KEYCHAIN",
    "TAIMEN_AGENT_ADAPTER": "CONTROL_PLANE_AGENT_ADAPTER",
    "TAIMEN_AGENT_WORKSPACE": "CONTROL_PLANE_AGENT_WORKSPACE",
    "TAIMEN_AGENT_POLL": "CONTROL_PLANE_AGENT_POLL",
}


def removed_environment_variables() -> dict[str, str]:
    """Legacy vars that are set in this process, mapped to their replacement."""
    return {
        name: replacement
        for name, replacement in _REMOVED_ENV_VARS.items()
        if os.environ.get(name) and not os.environ.get(replacement)
    }


def _config_base() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def _credentials_path() -> Path:
    return _config_base() / "control-plane" / "credentials.json"


def _normalize(server_url: str) -> str:
    return server_url.rstrip("/")


def _keychain_available() -> bool:
    if platform.system() != "Darwin":
        return False
    return os.environ.get("CONTROL_PLANE_NO_KEYCHAIN") != "1"


def _keychain_get(server_url: str, service: str = _KEYCHAIN_SERVICE) -> str | None:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                _normalize(server_url),
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    key = result.stdout.strip()
    return key if result.returncode == 0 and key else None


def _keychain_set(server_url: str, api_key: str) -> bool:
    """Store the key, feeding it through stdin rather than argv.

    ``security ... -w <key>`` would expose the plaintext key in the process
    table (``ps`` shows argv of other users' processes on macOS). With a bare
    ``-w`` the tool prompts for the password twice on stdin instead.
    """
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",  # update if exists
                "-s",
                _KEYCHAIN_SERVICE,
                "-a",
                _normalize(server_url),
                "-w",
            ],
            input=f"{api_key}\n{api_key}\n".encode(),
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    # The prompt path is interactive by nature: verify the value really landed.
    return _keychain_get(server_url) == api_key


def _keychain_delete(server_url: str, service: str = _KEYCHAIN_SERVICE) -> None:
    subprocess.run(
        [
            "security",
            "delete-generic-password",
            "-s",
            service,
            "-a",
            _normalize(server_url),
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )


def _file_load(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_store(data: dict[str, dict[str, str]]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=2) + "\n"
    # Create with 0600 from the start: writing then chmod'ing would leave a
    # window where the plaintext key file is world-readable at umask default.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    # Tighten an already-existing file whose mode predates this code.
    path.chmod(0o600)


class CredentialProvider(Protocol):
    """What the client presents as a Bearer, and how it is renewed.

    An API key is a constant, an IAM access token is not: it expires within
    minutes while a harness session runs for hours. Both are hidden behind this
    protocol so the transport does not need to know which one it holds.
    """

    async def token(self) -> str: ...

    async def refresh(self) -> str: ...

    @property
    def refreshable(self) -> bool: ...


class StaticCredential:
    """A credential that never changes: the legacy ``cp_`` API key."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def token(self) -> str:
        return self._api_key

    async def refresh(self) -> str:
        # Nothing to renew: a rejected key stays rejected, and pretending
        # otherwise would turn a single 401 into a retry loop.
        return self._api_key

    @property
    def refreshable(self) -> bool:
        return False


def resolve_credential(server_url: str) -> CredentialProvider | None:
    """Pick the harness credential: an IAM identity first, then the API key.

    IAM wins when it is configured, because during the compatibility window a
    machine may still hold both, and the newer identity is the one that was
    configured deliberately.
    """
    iam = iam_credential_from_environment()
    if iam is not None:
        return iam
    api_key = resolve_api_key(server_url)
    return StaticCredential(api_key) if api_key else None


def resolve_api_key(server_url: str) -> str | None:
    """Find an API key for the server, most explicit source first."""
    env = os.environ.get(_ENV_VAR)
    if env:
        return env.strip()
    if _keychain_available():
        key = _keychain_get(server_url)
        if key:
            return key
    entry = _file_load(_credentials_path()).get(_normalize(server_url))
    if entry and entry.get("apiKey"):
        return str(entry["apiKey"])
    return None


def store_api_key(server_url: str, api_key: str) -> str:
    """Persist a key in the most secure store available; returns store name."""
    if _keychain_available() and _keychain_set(server_url, api_key):
        return "keychain"
    data = _file_load(_credentials_path())
    data[_normalize(server_url)] = {"apiKey": api_key}
    _file_store(data)
    return str(_credentials_path())


def delete_api_key(server_url: str) -> None:
    if _keychain_available():
        _keychain_delete(server_url)
    path = _credentials_path()
    data = _file_load(path)
    if _normalize(server_url) in data:
        del data[_normalize(server_url)]
        _file_store(data)
