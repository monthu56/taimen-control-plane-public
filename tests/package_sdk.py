"""Where the contract tests take the catalog schema of packages from.

The schema belongs to package-sdk (``schema/v1``); since S007 the superproject
holds package-sdk as its submodule ``package-sdk`` and no longer has
``packages/schema``. The core keeps pinned copies in
``tests/fixtures/superproject/`` so that it is tested outside the superproject
too; inside it the pinned copies must equal package-sdk.

Inside the umbrella (a superproject whose ``.gitmodules`` declares
control-plane, flat layout) a missing package-sdk schema is an error: the
submodule is not checked out, and a skip would hide a drifted copy. Outside
the umbrella the tests fall back to the pinned copies.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINNED_SCHEMAS = ROOT / "tests" / "fixtures" / "superproject"
# In the superproject control-plane and package-sdk are submodules at its root.
UMBRELLA = ROOT.parent
PACKAGE_SDK_SCHEMAS = UMBRELLA / "package-sdk" / "schema" / "v1"
# The schemas the core pins; each one is held equal to package-sdk.
PINNED_NAMES = ("object.schema.json", "test.schema.json")

_SUBMODULE_PATH = re.compile(r"^\s*path\s*=\s*control-plane\s*$", re.MULTILINE)


def inside_umbrella() -> bool:
    """control-plane is a submodule of the directory above it."""
    gitmodules = UMBRELLA / ".gitmodules"
    return gitmodules.is_file() and bool(_SUBMODULE_PATH.search(gitmodules.read_text("utf-8")))


def schema_path(name: str) -> Path:
    """package-sdk's schema when it is checked out beside control-plane, else the pinned copy."""
    live = PACKAGE_SDK_SCHEMAS / name
    return live if live.is_file() else PINNED_SCHEMAS / name


def live_schema_path(name: str) -> Path:
    """package-sdk's schema; if absent, an error inside the umbrella and a skip outside it."""
    live = PACKAGE_SDK_SCHEMAS / name
    if live.is_file():
        return live
    if inside_umbrella():
        pytest.fail(
            f"{live.relative_to(UMBRELLA)} is missing in the superproject:"
            " check out the submodule package-sdk (git submodule update --init package-sdk)"
        )
    pytest.skip("package-sdk is not checked out next to control-plane")
