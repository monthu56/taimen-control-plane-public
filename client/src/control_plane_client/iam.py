"""IAM-backed credential for a local harness (IAM-7).

The operator signs in once with ``iam auth login``; the Platform Access Token
lands in the OS credential store. That token is never presented to the Control
Plane — it is presented only to IAM, and only in a request body. What the
Control Plane receives is a short-lived access token of its own audience.

That token lives about five minutes, while an MCP session lives for hours, so a
credential that is resolved once at startup would stop working mid-session.
Hence a provider rather than a string: it exchanges on demand, caches until
shortly before expiry, and re-exchanges when the server says the token is no
longer accepted.

The local PAT store format is IAM's contract (ADR-0012), not this package's.
It is read here — and never written — so that a harness does not need the IAM
package installed to use an IAM identity.

A record is addressed by ``issuer|tenant|principal``. The pair alone is not
enough where several executors of one tenant share a machine — their records
would collide, and the second would take over the first. When this process is
one of several, it says which Principal it is (``IAM_PRINCIPAL``); when the
machine holds several credentials and it says nothing, the answer is a refusal
rather than a guess. Guessing here means working under somebody else's
identity, which shows up only in audit.

A process that keeps its PAT elsewhere — a per-process secret file mounted
into a container, say — hands it in as ``platform_access_token``: a string, or
a callable that is asked on every exchange so a rotated file is picked up
without a restart. The local store is then never consulted, and the tenant is
informational only (the exchange request does not carry it).
"""

import json
import os
import platform
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from control_plane_client.errors import ControlPlaneError

ENV_IAM_URL = "CONTROL_PLANE_IAM_URL"
ENV_IAM_TENANT = "CONTROL_PLANE_IAM_TENANT"
ENV_IAM_AUDIENCE = "CONTROL_PLANE_IAM_AUDIENCE"
ENV_IAM_SCOPES = "CONTROL_PLANE_IAM_SCOPES"

# Owned by IAM's local credential store; mirrored here for reading only.
ENV_CREDENTIAL_MODE = "IAM_CREDENTIAL_MODE"
ENV_PLATFORM_ACCESS_TOKEN = "IAM_PLATFORM_ACCESS_TOKEN"
ENV_NO_KEYCHAIN = "IAM_NO_KEYCHAIN"
ENV_PRINCIPAL = "IAM_PRINCIPAL"
KEYCHAIN_SERVICE = "iam.platform-access-token"
# Section of the store listing who lives on this machine: names, no secrets.
# It exists because the OS credential store cannot be enumerated.
INDEX_KEY = "principals"
_ENVIRONMENT_MODES = frozenset({"environment", "ci"})

DEFAULT_AUDIENCE = "control-plane"
# Re-exchange this long before expiry: a token that expires in flight would
# surface as an authentication failure on an ordinary command.
DEFAULT_REFRESH_MARGIN_SECONDS = 30.0


class IamCredentialError(ControlPlaneError):
    """The IAM identity cannot be resolved or exchanged.

    ``status`` is the HTTP status IAM answered the exchange with, so a caller
    can tell a retryable 5xx/429 from a final 4xx without parsing the message;
    0 when there was no such answer (local store, unreachable IAM, malformed
    response).
    """

    def __init__(self, code: str, message: str, *, status: int = 0) -> None:
        super().__init__(code, message, status=status)


class IamCredential:
    """Exchanges a Platform Access Token for an audience-bound access token."""

    def __init__(
        self,
        iam_url: str,
        tenant_id: str,
        *,
        audience: str = DEFAULT_AUDIENCE,
        scopes: tuple[str, ...] = (),
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        refresh_margin_seconds: float = DEFAULT_REFRESH_MARGIN_SECONDS,
        clock: object | None = None,
        platform_access_token: str | Callable[[], str] | None = None,
    ) -> None:
        if not iam_url:
            raise IamCredentialError("iam_url_required", f"{ENV_IAM_URL} is not set")
        if not tenant_id and platform_access_token is None:
            # The tenant addresses the record in the local store; a PAT given
            # explicitly has no record to address.
            raise IamCredentialError("iam_tenant_required", f"{ENV_IAM_TENANT} is not set")
        self._iam_url = iam_url.rstrip("/")
        self._tenant_id = tenant_id
        self._audience = audience
        self._scopes = tuple(scopes)
        self._explicit_token = platform_access_token
        self._environ = os.environ if environ is None else environ
        self._transport = transport
        self._timeout = timeout
        self._margin = refresh_margin_seconds
        self._clock = clock if callable(clock) else time.monotonic
        self._token = ""
        self._expires_at = 0.0

    @property
    def refreshable(self) -> bool:
        return True

    @property
    def account(self) -> str:
        """Store key: one Principal may hold tokens for several IAM tenants."""
        return f"{self._iam_url}|{self._tenant_id}"

    @property
    def audience(self) -> str:
        """Which service the exchanged access token is for."""
        return self._audience

    @property
    def scopes(self) -> tuple[str, ...]:
        return self._scopes

    @property
    def principal_id(self) -> str:
        """Which executor of this machine this process is, if it says so."""
        return self._environ.get(ENV_PRINCIPAL, "").strip()

    async def token(self) -> str:
        if self._token and self._clock() + self._margin < self._expires_at:
            return self._token
        return await self.refresh()

    async def refresh(self) -> str:
        pat = self._platform_access_token()
        body = {"token": pat, "audience": self._audience, "scopes": list(self._scopes)}
        url = f"{self._iam_url}/api/v1/platform-access-tokens:exchange"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise IamCredentialError(
                "iam_unreachable", f"IAM is unreachable: {type(exc).__name__}"
            ) from exc

        if response.status_code == 401:
            # The login hint is right only for the local store: a token handed
            # in explicitly is renewed wherever it came from.
            hint = "" if self._explicit_token is not None else ": run `iam auth login`"
            raise IamCredentialError(
                "iam_invalid_token",
                f"IAM rejected the Platform Access Token{hint}",
                status=response.status_code,
            )
        if response.status_code == 403:
            raise IamCredentialError(
                "iam_audience_not_allowed",
                f"the token is not allowed for audience {self._audience}",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise IamCredentialError(
                "iam_exchange_failed",
                f"IAM answered {response.status_code} to the exchange",
                status=response.status_code,
            )

        try:
            payload = response.json()
            token = str(payload["accessToken"])
            expires_in = int(payload["expiresIn"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IamCredentialError(
                "iam_exchange_malformed", "IAM returned a malformed exchange response"
            ) from exc
        if not token or expires_in <= 0:
            raise IamCredentialError(
                "iam_exchange_malformed", "IAM returned an empty or expired credential"
            )

        self._token = token
        self._expires_at = self._clock() + expires_in
        return token

    # -- local PAT store (read-only mirror of IAM's contract) ------------------

    def _platform_access_token(self) -> str:
        if self._explicit_token is not None:
            explicit = self._explicit_token
            token = (explicit() if callable(explicit) else explicit).strip()
            if not token:
                raise IamCredentialError(
                    "iam_not_authenticated", "the Platform Access Token given explicitly is empty"
                )
            return token
        found = self._environment_token()
        if found:
            return found
        owner = self._owner()
        if self._keychain_available():
            found = self._keychain_get(owner)
            if found:
                return found
        found = self._file_token(owner)
        if found:
            return found
        raise IamCredentialError(
            "iam_not_authenticated",
            f"no Platform Access Token for {self.account}: run `iam auth login`",
        )

    def _owner(self) -> str:
        """Whose credential this process may use, resolved before any lookup.

        Declared wins. Undeclared is fine while the machine holds one identity
        for this tenant — and is refused once it holds several, because picking
        one would mean running as somebody else.
        """
        declared = self.principal_id
        if declared:
            return declared
        known = self._known_principals()
        if len(known) > 1:
            raise IamCredentialError(
                "iam_credential_ambiguous",
                f"several credentials for {self.account} on this machine: "
                f"set {ENV_PRINCIPAL} to the Principal this process runs as",
            )
        return known[0] if known else ""

    def _environment_token(self) -> str | None:
        token = self._environ.get(ENV_PLATFORM_ACCESS_TOKEN, "").strip()
        if not token:
            return None
        mode = self._environ.get(ENV_CREDENTIAL_MODE, "").strip().lower()
        if mode not in _ENVIRONMENT_MODES:
            # An inherited variable must not silently replace the developer's
            # credential, so an undeclared mode is an error, not a fallback.
            raise IamCredentialError(
                "iam_environment_mode_required",
                f"{ENV_PLATFORM_ACCESS_TOKEN} is set without {ENV_CREDENTIAL_MODE}=environment",
            )
        return token

    def _keychain_available(self) -> bool:
        if platform.system() != "Darwin":
            return False
        return self._environ.get(ENV_NO_KEYCHAIN) != "1"

    def _keychain_get(self, owner: str) -> str | None:
        for account in dict.fromkeys([f"{self.account}|{owner}" if owner else "", self.account]):
            if not account:
                continue
            try:
                result = subprocess.run(
                    [
                        "security",
                        "find-generic-password",
                        "-s",
                        KEYCHAIN_SERVICE,
                        "-a",
                        account,
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError:  # pragma: no cover - security(1) missing
                return None
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        return None

    def _credentials_path(self) -> Path:
        configured = self._environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(configured) if configured else Path.home() / ".config"
        return base / "iam" / "credentials.json"

    def _document(self) -> dict[str, object]:
        path = self._credentials_path()
        if not path.exists():
            return {}
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            # Readable by someone else: that is an incident, not a small
            # configuration slip, so the credential is not used.
            raise IamCredentialError(
                "iam_credentials_file_permissions",
                f"{path} has mode {mode:o}; 600 is expected",
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IamCredentialError(
                "iam_credentials_file_unreadable", f"{path} cannot be read"
            ) from exc
        return document if isinstance(document, dict) else {}

    def _known_principals(self) -> list[str]:
        """Who this machine holds a credential for, under this issuer+tenant."""
        document = self._document()
        found: list[str] = []
        index = document.get(INDEX_KEY)
        if isinstance(index, dict):
            listed = index.get(self.account)
            if isinstance(listed, list):
                found.extend(str(item) for item in listed if item)
        prefix = f"{self.account}|"
        for key, entry in document.items():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            if isinstance(entry, dict) and entry.get("token"):
                found.append(str(entry.get("principalId", "")) or key[len(prefix) :])
        return list(dict.fromkeys(found))

    def _file_token(self, owner: str) -> str | None:
        document = self._document()
        if owner:
            entry = document.get(f"{self.account}|{owner}")
            if isinstance(entry, dict) and entry.get("token"):
                return str(entry["token"])
        legacy = document.get(self.account)
        if isinstance(legacy, dict) and legacy.get("token"):
            recorded = str(legacy.get("principalId", ""))
            # An entry of the old format does not say whose it is. Using one
            # that names a different Principal would be the silent substitution
            # this guards against.
            if not owner or not recorded or recorded == owner:
                return str(legacy["token"])
        return None


def iam_credential_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> IamCredential | None:
    """Build an IAM credential when the harness is configured for one.

    Absence of ``CONTROL_PLANE_IAM_URL`` means "not configured" and hands the
    decision back to the legacy key. A URL without a tenant, however, is a
    broken configuration and says so instead of quietly falling back.
    """
    values = os.environ if environ is None else environ
    iam_url = values.get(ENV_IAM_URL, "").strip()
    if not iam_url:
        return None
    scopes = tuple(
        item for item in values.get(ENV_IAM_SCOPES, "").replace(",", " ").split() if item
    )
    return IamCredential(
        iam_url,
        values.get(ENV_IAM_TENANT, "").strip(),
        audience=values.get(ENV_IAM_AUDIENCE, "").strip() or DEFAULT_AUDIENCE,
        scopes=scopes,
        environ=values,
        transport=transport,
    )
