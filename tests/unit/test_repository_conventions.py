"""Conventions of this repository for the runner daemon (universal-runner U171).

``.agents/runner.yaml`` must pass the format of U003, and its setup and checks
must be the make targets CI calls, not copies of their commands: the daemon and
CI then cannot drift apart. ``CLAUDE.md`` only points at ``AGENTS.md``.
"""

import re
import tomllib
from pathlib import Path

import yaml

from control_plane_agent.runner_config import (
    RUNNER_CONFIG_PATH,
    RunnerConfig,
    ServiceCredentials,
    parse_runner_config,
)

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
MAKE_CALL = re.compile(r"^make ([a-z][a-z0-9-]*)(?: [A-Z_]+=\S*)*$")


def _config() -> RunnerConfig:
    return parse_runner_config((REPO / RUNNER_CONFIG_PATH).read_bytes())


def _make_target(command: str) -> str:
    match = MAKE_CALL.match(command.strip())
    assert match, f"not a single make target: {command!r}"
    return match.group(1)


def _ci_runs(job: str) -> list[str]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    return [step["run"] for step in workflow["jobs"][job]["steps"] if "run" in step]


def _makefile_targets() -> set[str]:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z][a-z0-9-]*):", text, re.MULTILINE))


def test_runner_config_passes_the_format() -> None:
    config = _config()

    assert config.version == 1
    assert [check.name for check in config.checks] == ["lint", "types", "tests"]


def test_setup_and_checks_are_make_targets_of_the_makefile() -> None:
    config = _config()
    assert config.setup is not None
    commands = [config.setup, *(check.run for check in config.checks)]

    targets = {_make_target(command) for command in commands}

    assert targets <= _makefile_targets()


def test_ci_calls_the_same_targets() -> None:
    config = _config()
    assert config.setup is not None
    runner_targets = {_make_target(c) for c in [config.setup, *(ch.run for ch in config.checks)]}

    full_suite = {_make_target(run) for run in _ci_runs("checks") if run.startswith("make ")}
    fast = {_make_target(run) for run in _ci_runs("lint") if run.startswith("make ")}

    assert runner_targets <= full_suite
    assert {"install", "lint", "typecheck"} <= fast


def test_ci_keeps_no_copies_of_the_target_commands() -> None:
    for job in ("lint", "checks"):
        copies = [run for run in _ci_runs(job) if re.search(r"\buv (run|sync)\b", run)]
        assert not copies, f"{job}: call make targets instead of {copies}"


def test_tests_get_the_database_from_the_service() -> None:
    config = _config()
    tests = next(check for check in config.checks if check.name == "tests")
    credentials = ServiceCredentials(host="db", port=5432, user="u", password="p", database="d")

    envs = [config.services[name].render_env(credentials) for name in tests.services]

    assert {"CP_TEST_DATABASE_URL": "postgresql+psycopg://u:p@db:5432/d"} in envs


def test_path_dependencies_are_neighbours() -> None:
    manifest = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    sources = manifest["tool"]["uv"]["sources"].values()
    outside = {Path(s["path"]).name for s in sources if s.get("path", "").startswith("../")}

    assert outside
    assert outside <= set(_config().neighbours)


def test_claude_md_points_at_agents_md() -> None:
    assert (REPO / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert (REPO / "AGENTS.md").is_file()


def _agents_md() -> str:
    return " ".join((REPO / "AGENTS.md").read_text(encoding="utf-8").split())


def test_agents_md_yields_the_database_address_to_agent_instructions() -> None:
    # Until the daemon reads runner.yaml, an executor may be told by its own
    # instructions to use another test database; AGENTS.md must not forbid it.
    text = _agents_md()

    assert "Другой адрес базы не подставляй" not in text
    assert "Если инструкции агента задают свой адрес — следуй им" in text
    assert "чужие базы не трогай" in text.lower()


def test_agents_md_does_not_name_who_reviews() -> None:
    assert "Ревью делает человек" not in _agents_md()
