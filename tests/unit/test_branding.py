"""White-label guard: the historical product codename must not creep back.

v0.5 closed the compatibility window (ADR-0040). The codename now survives
ONLY in immutable history — decision records — and in this guard. Anything
else is a regression, and so is a *removed* entry point coming back to life.
"""

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CODENAME = re.compile(r"taimen", re.IGNORECASE)

# Files allowed to mention the codename — each for a documented reason.
ALLOWED = {
    # Historical decision records (immutable) and the removal ADR itself.
    "docs/adr/0016-claude-code-mcp-integration.md",
    "docs/adr/0017-local-harness-authentication.md",
    "docs/adr/0022-white-label-naming.md",
    "docs/adr/0040-end-of-compatibility-window.md",
    # Names the product repository and staging principal it describes; the
    # decision is about a deployment, not about core contracts (ADR-0022 §scope).
    "docs/adr/0052-auto-review-by-runner-daemon.md",
    # The migration note has to name what was removed to be useful.
    "docs/migration-v0.5.md",
    # The single place that still knows the removed names, purely to produce
    # an actionable error for a stale environment.
    "client/src/control_plane_client/credentials.py",
    # The removed project-config directory, named only in the diagnostic.
    "client/src/control_plane_client/config.py",
    # This guard itself and the test that proves the removal.
    "tests/unit/test_branding.py",
    "tests/client/test_cli.py",
    # Project-level community and licence documents of the open-source release
    # name the project they belong to; they are not core contracts (ADR-0022).
    "CONTRIBUTING.md",
    "SECURITY.md",
    "TRADEMARK.md",
}

SCAN_GLOBS = [
    "src/**/*.py",
    "client/src/**/*.py",
    "docs/**/*.md",
    "*.md",
    "pyproject.toml",
    "docker-compose.yml",
    "migrations/**/*.py",
    "tests/**/*.py",
    "scripts/**/*.py",
    "Makefile",
    "Dockerfile",
    ".env.example",
    ".agents/*.yaml",
]


def test_codename_absent_outside_allowlist() -> None:
    offenders: list[str] = []
    for pattern in SCAN_GLOBS:
        for path in REPO.glob(pattern):
            rel = path.relative_to(REPO).as_posix()
            if rel in ALLOWED or not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if CODENAME.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()[:80]}")
    assert not offenders, "codename found outside the allowlist:\n" + "\n".join(offenders)


def test_allowlisted_anchors_still_exist() -> None:
    """When an allowlisted file goes away, drop it from ALLOWED too."""
    for rel in sorted(ALLOWED):
        assert (REPO / rel).exists(), f"allowlisted file vanished: {rel}"


def test_removed_entry_points_are_gone() -> None:
    """The v0.5 removal must stay removed (ADR-0040)."""
    assert not (REPO / "src" / "taimen_client").exists(), "the import shim is back"

    manifest = tomllib.loads((REPO / "pyproject.toml").read_text())
    scripts = manifest["project"]["scripts"]
    assert not [name for name in scripts if CODENAME.search(name)], scripts
    packages = manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert not [name for name in packages if CODENAME.search(name)], packages


def test_removed_shim_is_not_importable() -> None:
    import importlib

    try:
        importlib.import_module("taimen_client")
    except ModuleNotFoundError:
        return
    raise AssertionError("the deprecated taimen_client package is still importable")


def test_sdk_exports_no_codename_aliases() -> None:
    import control_plane_client
    from control_plane_client import errors

    assert not [name for name in control_plane_client.__all__ if CODENAME.search(name)]
    assert not [name for name in dir(control_plane_client) if CODENAME.search(name)]
    assert not [name for name in dir(errors) if CODENAME.search(name)]


def test_bootstrap_context_announces_no_legacy_protocol_name() -> None:
    from control_plane.domain import enums

    assert enums.HARNESS_PROTOCOL_NAME == "control-harness"
    assert not [name for name in dir(enums) if "LEGACY_HARNESS" in name]
