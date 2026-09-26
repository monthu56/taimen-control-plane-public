"""CLI smoke tests: parser wiring, project config, credential store."""

import os
from pathlib import Path

import pytest

from control_plane_cli.main import build_parser
from control_plane_client import config as project_config
from control_plane_client import credentials


def test_parser_covers_documented_commands() -> None:
    parser = build_parser()
    for argv in (
        ["whoami"],
        ["context"],
        ["work", "list"],
        ["task", "get", "TASK-000001"],
        ["task", "claimability", "TASK-000001"],
        ["task", "claim", "TASK-000001"],
        ["run", "start", "TASK-000001", "--claim", "c", "--fencing-token", "1"],
        ["run", "status", "r"],
        ["artifact", "add", "--type", "file", "--name", "x"],
        ["approvals", "list"],
        ["events", "tail"],
        ["login"],
        ["logout"],
        ["init", "--server", "http://x"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func), argv


def test_project_config_roundtrip(tmp_path: Path) -> None:
    nested = tmp_path / "repo" / "src" / "deep"
    nested.mkdir(parents=True)
    path = project_config.write_project_config(
        tmp_path / "repo",
        server="http://cp.local/",
        workspace="engineering",
        repository="git@github.com:acme/x.git",
    )
    assert path.name == "config.json"

    found = project_config.find_project_config(nested)
    assert found is not None
    assert found.server == "http://cp.local"  # trailing slash normalized
    assert found.workspace == "engineering"
    assert project_config.find_project_config(tmp_path) is None  # outside the repo


def test_project_config_search_stops_at_repo_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated ancestor config must not decide where credentials go."""
    home = tmp_path / "home"
    repo = home / "work" / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    project_config.write_project_config(home, server="http://unrelated.example")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # Inside a repo without its own config: the ancestor config is ignored.
    assert project_config.find_project_config(repo / "src") is None

    # The repo's own config still wins.
    project_config.write_project_config(repo, server="http://correct.example")
    found = project_config.find_project_config(repo / "src")
    assert found is not None and found.server == "http://correct.example"


def test_credential_file_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CONTROL_PLANE_NO_KEYCHAIN", "1")
    for var in ("CONTROL_PLANE_API_KEY", "TAIMEN_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    assert credentials.resolve_api_key("http://s1") is None
    location = credentials.store_api_key("http://s1/", "cp_abc_secret")
    assert location.endswith("credentials.json")
    stored = Path(location)
    assert stored.stat().st_mode & 0o777 == 0o600
    assert "cp_abc_secret" in stored.read_text()

    assert credentials.resolve_api_key("http://s1") == "cp_abc_secret"
    # Env var wins over the file.
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "cp_env_key")
    assert credentials.resolve_api_key("http://s1") == "cp_env_key"
    monkeypatch.delenv("CONTROL_PLANE_API_KEY")

    credentials.delete_api_key("http://s1")
    assert credentials.resolve_api_key("http://s1") is None


def test_credential_file_never_world_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plaintext key file must be 0600 from creation, not after a chmod."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CONTROL_PLANE_NO_KEYCHAIN", "1")
    for var in ("CONTROL_PLANE_API_KEY", "TAIMEN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    original_umask = os.umask(0o022)  # a permissive umask must not leak through
    try:
        location = Path(credentials.store_api_key("http://s1", "cp_abc_secret"))
    finally:
        os.umask(original_umask)
    assert location.stat().st_mode & 0o077 == 0


def test_legacy_credential_locations_are_no_longer_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.5 closed the compatibility window (ADR-0040): the old file is dead."""
    import json

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CONTROL_PLANE_NO_KEYCHAIN", "1")
    for var in ("CONTROL_PLANE_API_KEY", "TAIMEN_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    legacy = tmp_path / "taimen" / "credentials.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"http://s1": {"apiKey": "cp_old_secret"}}))
    assert credentials.resolve_api_key("http://s1") is None


def test_removed_environment_variables_are_reported_not_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale env var must produce an actionable error, not silence."""
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    monkeypatch.setenv("TAIMEN_API_KEY", "cp_old_secret")
    monkeypatch.setenv("CONTROL_PLANE_NO_KEYCHAIN", "1")
    assert credentials.resolve_api_key("http://s1") is None
    assert credentials.removed_environment_variables() == {
        "TAIMEN_API_KEY": "CONTROL_PLANE_API_KEY"
    }
    # Once the neutral variable is set, the stale one stops being reported.
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "cp_new_secret")
    assert credentials.removed_environment_variables() == {}


def test_legacy_project_config_raises_a_migration_error(tmp_path: Path) -> None:
    """.taimen/config.json is not read, and not silently ignored either."""
    import json

    from control_plane_client import LegacyProjectConfigError

    repo = tmp_path / "repo"
    legacy_dir = repo / ".taimen"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "config.json").write_text(json.dumps({"server": "http://legacy.local"}))
    with pytest.raises(LegacyProjectConfigError):
        project_config.find_project_config(repo)


def test_project_binding_round_trips_the_project_field(tmp_path: Path) -> None:
    path = project_config.write_project_config(
        tmp_path, server="http://s1", workspace="ws-1", project="proj-1"
    )
    assert path.exists()
    found = project_config.find_project_config(tmp_path)
    assert found is not None
    assert found.project == "proj-1"
    assert found.workspace == "ws-1"
