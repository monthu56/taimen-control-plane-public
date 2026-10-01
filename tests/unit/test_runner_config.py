"""The ``.agents/runner.yaml`` format (universal-runner U003).

A valid file parses into what the daemon acts on; an invalid one is refused
with every error named by the path of its field; ``env`` placeholders are the
five the node hands out and nothing else.
"""

import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from control_plane_agent.runner_config import (
    NAME_RULES,
    PLACEHOLDERS,
    RESERVED_ENV_NAMES,
    SCHEMA_FILE,
    Check,
    FieldError,
    RunnerConfigError,
    ServiceCredentials,
    ServiceSpec,
    field_path,
    is_reserved_env_name,
    parse_runner_config,
    reserved_entry,
    run_environment,
    runner_config_schema,
    validate_runner_config,
)

VALID = """\
version: 1
neighbours: [platform-auth-sdk, memory-service]
setup: uv sync --frozen
services:
  db:
    template: postgres-16
    env:
      CP_TEST_DATABASE_URL: "postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"
checks:
  - {name: lint, run: make lint}
  - {name: types, run: make typecheck}
  - {name: tests, run: make test, services: [db]}
"""

MODULE = SCHEMA_FILE.with_name("runner_config.py")

CREDENTIALS = ServiceCredentials(
    host="svc-db-1", port=5432, user="u_7f", password="s3cr3t", database="d_7f"
)


def errors_of(text: str) -> list[FieldError]:
    with pytest.raises(RunnerConfigError) as caught:
        parse_runner_config(text)
    return caught.value.errors


def paths_of(text: str) -> list[str]:
    return [e.path for e in errors_of(text)]


# --- schema ---------------------------------------------------------------------------------


def test_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(runner_config_schema())


def test_schema_and_page_sit_beside_the_parser():
    page = SCHEMA_FILE.with_name("runner_config.md")
    assert page.is_file()
    # The page documents every top-level field and every placeholder.
    text = page.read_text(encoding="utf-8")
    for name in runner_config_schema()["properties"]:
        assert f"`{name}`" in text
    for name in PLACEHOLDERS:
        assert f"`{{{name}}}`" in text


def test_example_of_the_page_is_valid():
    page = SCHEMA_FILE.with_name("runner_config.md").read_text(encoding="utf-8")
    example = re.search(r"```yaml\n(.*?)```", page, re.S)
    assert example is not None
    assert parse_runner_config(example.group(1)).version == 1


# --- valid ----------------------------------------------------------------------------------


def test_valid_file_parses():
    config = parse_runner_config(VALID)

    assert config.version == 1
    assert config.neighbours == ("platform-auth-sdk", "memory-service")
    assert config.setup == "uv sync --frozen"
    assert set(config.services) == {"db"}
    assert config.services["db"].template == "postgres-16"
    assert config.checks == (
        Check(name="lint", run="make lint"),
        Check(name="types", run="make typecheck"),
        Check(name="tests", run="make test", services=("db",)),
    )


def test_minimal_file_has_empty_defaults():
    config = parse_runner_config("version: 1\n")

    assert config.neighbours == ()
    assert config.setup is None
    assert dict(config.services) == {}
    assert config.checks == ()


def test_bytes_are_accepted_like_text():
    assert parse_runner_config(VALID.encode()) == parse_runner_config(VALID)


def test_empty_sections_and_null_env_are_empty():
    config = parse_runner_config(
        "version: 1\nneighbours: []\nchecks: []\nservices:\n  db: {template: postgres-16}\n"
    )
    assert dict(config.services["db"].env) == {}
    assert config.services["db"].render_env(CREDENTIALS) == {}


# --- unknown fields -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("version: 1\nimage: postgres:16\n", "$.image"),
        (
            "version: 1\nservices:\n  db: {template: postgres-16, image: postgres:16}\n",
            "$.services.db.image",
        ),
        (
            "version: 1\nservices:\n  db: {template: pg, privileged: true}\n",
            "$.services.db.privileged",
        ),
        ("version: 1\nchecks:\n  - {name: t, run: make test, timeout: 5}\n", "$.checks[0].timeout"),
    ],
)
def test_unknown_field_is_named_by_its_path(text, path):
    errors = errors_of(text)

    assert errors == [FieldError(path, "unknown field")]


def test_every_unknown_field_is_reported():
    assert paths_of("version: 1\nfoo: 1\nbar: 2\n") == ["$.bar", "$.foo"]


# --- services without template --------------------------------------------------------------


def test_service_without_template_is_named_by_its_path():
    errors = errors_of("version: 1\nservices:\n  db:\n    env: {URL: '{host}'}\n")

    assert errors == [FieldError("$.services.db.template", "required field is missing")]
    assert "$.services.db.template: required field is missing" in str(RunnerConfigError(errors))


@pytest.mark.parametrize("service", ["null", "{}", "postgres-16", "[postgres-16]"])
def test_service_of_wrong_shape_is_refused(service):
    paths = paths_of(f"version: 1\nservices:\n  db: {service}\n")
    assert paths
    assert all(p.startswith("$.services.db") for p in paths)


def test_empty_template_is_refused():
    assert paths_of("version: 1\nservices:\n  db: {template: ''}\n") == ["$.services.db.template"]


# --- placeholders ---------------------------------------------------------------------------


def test_placeholders_are_filled_from_the_handed_out_service():
    config = parse_runner_config(VALID)

    env = config.services["db"].render_env(CREDENTIALS)

    assert env == {"CP_TEST_DATABASE_URL": "postgresql+psycopg://u_7f:s3cr3t@svc-db-1:5432/d_7f"}


@pytest.mark.parametrize("name", PLACEHOLDERS)
def test_each_placeholder_alone(name):
    config = parse_runner_config(
        f"version: 1\nservices:\n  db:\n    template: pg\n    env: {{V: 'x{{{name}}}y'}}\n"
    )
    expected = {"user": "u_7f", "password": "s3cr3t", "host": "svc-db-1", "port": "5432"}
    expected["database"] = "d_7f"

    assert config.services["db"].render_env(CREDENTIALS) == {"V": f"x{expected[name]}y"}


def test_doubled_braces_are_literal_and_values_are_not_reformatted():
    config = parse_runner_config(
        "version: 1\nservices:\n  db:\n    template: pg\n"
        '    env: {JSON: \'{{"db": "{database}"}}\', PLAIN: no-braces}\n'
    )
    tricky = ServiceCredentials(host="h", port=1, user="{password}", password="p}{", database="d")

    env = config.services["db"].render_env(tricky)

    assert env == {"JSON": '{"db": "d"}', "PLAIN": "no-braces"}
    # A handed-out value that looks like a placeholder is not expanded again.
    other = parse_runner_config("version: 1\nservices:\n  db: {template: pg, env: {U: '{user}'}}\n")
    assert other.services["db"].render_env(tricky) == {"U": "{password}"}


@pytest.mark.parametrize(
    "value",
    ["{name}", "{}", "{0}", "{port:d}", "{user!r}", "{user.upper}", "{password[0]}", "{USER}"],
)
def test_unknown_placeholder_is_named_by_its_path(value):
    errors = errors_of(
        f"version: 1\nservices:\n  db:\n    template: pg\n    env: {{DATABASE_URL: '{value}'}}\n"
    )

    assert [e.path for e in errors] == ["$.services.db.env.DATABASE_URL"]
    assert "unknown placeholder" in errors[0].message
    assert "{user} {password} {host} {port} {database}" in errors[0].message


@pytest.mark.parametrize("value", ["{user", "user}", "{host}:{port"])
def test_unbalanced_brace_is_refused(value):
    errors = errors_of(
        f"version: 1\nservices:\n  db:\n    template: pg\n    env: {{U: '{value}'}}\n"
    )

    assert [e.path for e in errors] == ["$.services.db.env.U"]
    assert "literal braces" in errors[0].message


def test_non_string_env_value_is_refused():
    assert paths_of("version: 1\nservices:\n  db: {template: pg, env: {PORT: 5432}}\n") == [
        "$.services.db.env.PORT"
    ]


def test_credentials_repr_hides_the_password():
    assert "s3cr3t" not in repr(CREDENTIALS)


# --- other errors ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("neighbours: []\n", "$.version"),
        ("version: 2\n", "$.version"),
        ("version: '1'\n", "$.version"),
        ("version: 1.5\n", "$.version"),
        ("version: true\n", "$.version"),
        ("version: null\n", "$.version"),
    ],
)
def test_version_is_required_and_only_1(text, path):
    assert paths_of(text) == [path]


def test_float_version_equal_to_1_is_refused():
    assert paths_of("version: 1.0\n") == ["$.version"]


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("version: 1\nneighbours: platform-auth-sdk\n", "$.neighbours"),
        ("version: 1\nneighbours: [a, a]\n", "$.neighbours"),
        ("version: 1\nneighbours: [https://example.com/a.git]\n", "$.neighbours[0]"),
        ("version: 1\nneighbours: [null]\n", "$.neighbours[0]"),
        ("version: 1\nsetup: ''\n", "$.setup"),
        ("version: 1\nsetup: [uv, sync]\n", "$.setup"),
        ("version: 1\nservices: []\n", "$.services"),
        ("version: 1\nservices:\n  Db: {template: pg}\n", "$.services.Db"),
        ("version: 1\nservices:\n  db: {template: pg, env: {'A-B': x}}\n", "$.services.db.env.A-B"),
        ("version: 1\nchecks:\n  - {run: make test}\n", "$.checks[0].name"),
        ("version: 1\nchecks:\n  - {name: t}\n", "$.checks[0].run"),
        ("version: 1\nchecks:\n  - {name: t, run: ''}\n", "$.checks[0].run"),
        ("version: 1\nchecks: {lint: make lint}\n", "$.checks"),
    ],
)
def test_error_is_named_by_the_path_of_its_field(text, path):
    assert paths_of(text) == [path]


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("version: 1\nservices:\n  db: {template: pg, env: {123: x}}\n", "$.services.db.env[123]"),
        ("version: 1\nservices:\n  null: {template: pg}\n", '$.services["None"]'),
        ("version: 1\ntrue: 1\n", '$["True"]'),
    ],
)
def test_key_that_is_not_a_string_is_refused(text, path):
    assert errors_of(text) == [FieldError(path, "a key must be a string")]


def test_check_naming_an_undeclared_service_is_refused():
    errors = errors_of(
        "version: 1\nservices:\n  db: {template: pg}\n"
        "checks:\n  - {name: t, run: make test, services: [db, cache]}\n"
    )

    assert errors == [FieldError("$.checks[0].services[1]", "no service 'cache' in services")]


def test_duplicate_check_name_is_refused():
    errors = errors_of("version: 1\nchecks:\n  - {name: t, run: a}\n  - {name: t, run: b}\n")

    assert errors == [FieldError("$.checks[1].name", "duplicate check name")]


def test_one_variable_is_set_by_one_service():
    errors = errors_of(
        "version: 1\nservices:\n"
        "  a: {template: pg, env: {URL: '{host}'}}\n"
        "  b: {template: pg, env: {URL: '{host}'}}\n"
    )

    assert errors == [FieldError("$.services.b.env.URL", "variable is already set by service 'a'")]


@pytest.mark.parametrize(
    ("text", "message"),
    [("", "the file is empty"), ("# only a comment\n", "the file is empty")],
)
def test_empty_file_is_refused(text, message):
    assert errors_of(text) == [FieldError("$", message)]


@pytest.mark.parametrize("text", ["- version: 1\n", "version 1\n", "42\n"])
def test_root_that_is_not_a_mapping_is_refused(text):
    assert paths_of(text) == ["$"]


def test_malformed_yaml_names_line_and_column():
    errors = errors_of("version: 1\nservices: {db: [\n")

    assert len(errors) == 1
    assert errors[0].path == "$"
    assert "not valid YAML at line" in errors[0].message


def test_yaml_tags_are_not_executed():
    errors = errors_of("version: !!python/object/apply:os.system ['true']\n")

    assert [e.path for e in errors] == ["$"]


def test_validation_is_idempotent_and_does_not_mutate_the_document():
    document = {"version": 1, "services": {"db": {"template": "pg", "env": {"U": "{user}"}}}}
    snapshot = repr(document)

    assert validate_runner_config(document) == validate_runner_config(document) == []
    assert repr(document) == snapshot


@pytest.mark.parametrize(
    ("parts", "path"),
    [
        ([], "$"),
        (["services", "db", "env", "URL"], "$.services.db.env.URL"),
        (["checks", 0, "run"], "$.checks[0].run"),
        (["services", "a.b"], '$.services["a.b"]'),
    ],
)
def test_field_path(parts, path):
    assert field_path(parts) == path


def test_schema_file_is_packaged_with_the_module():
    assert SCHEMA_FILE.parent == Path(__import__("control_plane_agent").__file__).parent


# --- names follow the fleet contract (review of 343ef67) ------------------------------------

LONG_SERVICE = "s" * 32


@pytest.mark.parametrize(
    "name", ["db", "d", "0", "db-main", "a1-b2", "s" * 31, "x" + "-" * 29 + "y"]
)
def test_service_name_that_fleet_accepts_is_accepted(name):
    config = parse_runner_config(f"version: 1\nservices:\n  '{name}': {{template: pg}}\n")
    assert set(config.services) == {name}


@pytest.mark.parametrize("name", ["db_main", "db.main", "db-", "-db", "Db", LONG_SERVICE])
def test_service_name_that_fleet_refuses_is_refused(name):
    paths = paths_of(f"version: 1\nservices:\n  '{name}': {{template: pg}}\n")
    assert paths == [field_path(["services", name])]


@pytest.mark.parametrize("name", ["postgres-16", "pg", "0", "graph-db", "t" * 63, "pg-"])
def test_template_name_that_fleet_accepts_is_accepted(name):
    config = parse_runner_config(f"version: 1\nservices:\n  db: {{template: '{name}'}}\n")
    assert config.services["db"].template == name


@pytest.mark.parametrize("name", ["postgres.16", "pg_16", "-pg", "Pg", "t" * 64])
def test_template_name_that_fleet_refuses_is_refused(name):
    assert paths_of(f"version: 1\nservices:\n  db: {{template: '{name}'}}\n") == [
        "$.services.db.template"
    ]


def test_check_naming_a_service_by_an_invalid_name_is_refused():
    assert paths_of(
        "version: 1\nservices:\n  db: {template: pg}\n"
        "checks:\n  - {name: t, run: make test, services: [db_main]}\n"
    ) == ["$.checks[0].services[0]"]


@pytest.mark.parametrize("key", ["platform-auth-sdk", "memory-service", "a", "k" * 63])
def test_neighbour_key_like_a_repository_key_is_accepted(key):
    assert parse_runner_config(f"version: 1\nneighbours: ['{key}']\n").neighbours == (key,)


@pytest.mark.parametrize("key", ["auth.sdk", "auth_sdk", "-auth", "Auth", "k" * 64])
def test_neighbour_key_unlike_a_repository_key_is_refused(key):
    assert paths_of(f"version: 1\nneighbours: ['{key}']\n") == ["$.neighbours[0]"]


# --- a trailing newline does not pass for the end of a name (review of 343ef67) -------------


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ('version: 1\nservices:\n  "db\\n": {template: pg}\n', '$.services["db\\n"]'),
        ('version: 1\nservices:\n  db: {template: "postgres-16\\n"}\n', "$.services.db.template"),
        (
            'version: 1\nservices:\n  db: {template: pg, env: {"URL\\n": "{host}"}}\n',
            '$.services.db.env["URL\\n"]',
        ),
        ('version: 1\nneighbours: ["memory-service\\n"]\n', "$.neighbours[0]"),
        ('version: 1\nchecks:\n  - {name: "lint\\n", run: make lint}\n', "$.checks[0].name"),
        (
            "version: 1\nservices:\n  db: {template: pg}\n"
            'checks:\n  - {name: t, run: make test, services: ["db\\n"]}\n',
            "$.checks[0].services[0]",
        ),
    ],
)
def test_trailing_newline_is_not_the_end_of_a_name(text, path):
    assert paths_of(text) == [path]


def test_every_pattern_of_the_schema_refuses_a_trailing_newline():
    for name, definition in runner_config_schema()["$defs"].items():
        pattern = definition.get("pattern")
        if pattern is not None and name != "reservedEnvName":
            assert pattern.endswith("(?![\\s\\S])"), name
            assert re.search(pattern, "a\n") is None, name


# --- reserved env names ---------------------------------------------------------------------


#: One name for every entry of RESERVED_ENV_NAMES (review of U003: a test per entry).
RESERVED_SAMPLES = {
    "PATH": ["PATH"],
    "HOME": ["HOME"],
    "SHELL": ["SHELL"],
    "ENV": ["ENV"],
    "BASH_ENV": ["BASH_ENV"],
    "PYTHONPATH": ["PYTHONPATH"],
    "PYTHONHOME": ["PYTHONHOME"],
    "PYTHONSTARTUP": ["PYTHONSTARTUP"],
    "PYTHONUSERBASE": ["PYTHONUSERBASE"],
    "NODE_OPTIONS": ["NODE_OPTIONS"],
    "NODE_PATH": ["NODE_PATH"],
    "NODE_EXTRA_CA_CERTS": ["NODE_EXTRA_CA_CERTS"],
    "NODE_TLS_REJECT_UNAUTHORIZED": ["NODE_TLS_REJECT_UNAUTHORIZED"],
    "PERL5OPT": ["PERL5OPT"],
    "RUBYOPT": ["RUBYOPT"],
    "JAVA_TOOL_OPTIONS": ["JAVA_TOOL_OPTIONS"],
    "_JAVA_OPTIONS": ["_JAVA_OPTIONS"],
    "SSL_CERT_FILE": ["SSL_CERT_FILE"],
    "SSL_CERT_DIR": ["SSL_CERT_DIR"],
    "REQUESTS_CA_BUNDLE": ["REQUESTS_CA_BUNDLE"],
    "CURL_CA_BUNDLE": ["CURL_CA_BUNDLE"],
    "CURL_HOME": ["CURL_HOME"],
    "LD_*": ["LD_PRELOAD", "LD_LIBRARY_PATH"],
    "DYLD_*": ["DYLD_INSERT_LIBRARIES"],
    # The client looks its credentials file up by XDG_CONFIG_HOME: a repository
    # pointing it into the copy would swap the identity of the MCP plugin.
    "XDG_*": ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"],
    "GIT_*": ["GIT_DIR", "GIT_SSH_COMMAND"],
    "GH_*": ["GH_TOKEN", "GH_HOST"],
    "SSH_*": ["SSH_AUTH_SOCK"],
    "UV_*": ["UV_INDEX_URL"],
    "PIP_*": ["PIP_INDEX_URL", "PIP_CONFIG_FILE"],
    "NPM_CONFIG_*": ["NPM_CONFIG_REGISTRY", "npm_config_userconfig", "Npm_Config_Registry"],
    "FLEET_*": ["FLEET_SERVICES_URL"],
    "CLAUDE_*": ["CLAUDE_CONFIG_DIR"],
    "ANTHROPIC_*": ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"],
    "OPENAI_*": ["OPENAI_BASE_URL"],
    "CODEX_*": ["CODEX_HOME"],
    "OPENCODE_*": ["OPENCODE_SERVER_PASSWORD"],
    "IAM_*": ["IAM_CREDENTIAL_MODE", "IAM_PLATFORM_ACCESS_TOKEN"],
    "CONTROL_PLANE_*": ["CONTROL_PLANE_TOKEN", "CONTROL_PLANE_RUN_ID"],
    "*_PROXY": ["HTTPS_PROXY", "http_proxy", "ALL_PROXY", "no_proxy", "Ftp_Proxy"],
}
RESERVED_CASES = [(name, entry) for entry, names in RESERVED_SAMPLES.items() for name in names]
LOOKALIKES = [
    "PATHS",
    "MY_PATH",
    "HOMEDIR",
    "LDX",
    "GITHUB_TOKEN_FILE",
    "FLEET",
    "path",
    "home",
    "env",
    "ENVIRONMENT",
    "SHELLCHECK_OPTS",
    "NODE_ENV",
    "PYTHONUNBUFFERED",
    "UVICORN_PORT",
    "SSHD",
    "IAMX",
    "anthropic_base_url",
    "PROXY",
    "MY_PROXY_URL",
    "CP_TEST_DATABASE_URL",
    "XDG",
    "XDGX_HOME",
    "xdg_config_home",
    "GHOST",
    "GITHUB_SHA",
    "PIPELINE_ID",
    "NPM_TOKEN",
    "SSL_MODE",
    "PGSSLROOTCERT",
    "CURL",
    "JAVA_HOME",
    "RUBYOPTS",
]


def test_every_reserved_entry_has_a_test() -> None:
    assert list(RESERVED_SAMPLES) == list(RESERVED_ENV_NAMES)


@pytest.mark.parametrize(("name", "entry"), RESERVED_CASES)
def test_reserved_env_name_is_refused(name, entry):
    errors = errors_of(f"version: 1\nservices:\n  db: {{template: pg, env: {{{name}: x}}}}\n")

    assert errors == [FieldError(f"$.services.db.env.{name}", f"reserved variable name ({entry})")]


@pytest.mark.parametrize(("name", "entry"), RESERVED_CASES)
def test_schema_and_the_list_agree_on_reserved_names(name, entry):
    assert is_reserved_env_name(name)
    assert reserved_entry(name) == entry


@pytest.mark.parametrize("name", LOOKALIKES)
def test_schema_and_the_list_agree_on_ordinary_names(name):
    assert not is_reserved_env_name(name)
    assert reserved_entry(name) is None


@pytest.mark.parametrize("name", LOOKALIKES)
def test_env_name_that_only_resembles_a_reserved_one_is_accepted(name):
    config = parse_runner_config(
        f"version: 1\nservices:\n  db: {{template: pg, env: {{{name}: x}}}}\n"
    )
    assert dict(config.services["db"].env) == {name: "x"}


# --- the environment of a run, filtered again by the adapter (U009) -------------------------


def test_run_environment_keeps_ordinary_names_in_a_fresh_dict():
    env = {"CP_TEST_DATABASE_URL": "postgresql://u:p@h:1/d", "NODE_ENV": "test"}

    result = run_environment(env)

    assert result == env
    assert result is not env


@pytest.mark.parametrize("env", [None, {}])
def test_run_environment_of_nothing_is_empty(env):
    assert run_environment(env) == {}


@pytest.mark.parametrize(("name", "entry"), RESERVED_CASES)
def test_run_environment_drops_reserved_names(name, entry):
    assert run_environment({name: "x", "KEPT": "y"}) == {"KEPT": "y"}


@pytest.mark.parametrize("name", ["", "1URL", "A-B", "URL\n", "Ü", "A" * 129, 5, None])
def test_run_environment_drops_malformed_names(name):
    assert run_environment({name: "x", "KEPT": "y"}) == {"KEPT": "y"}  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [None, 5432, b"x", ["x"]])
def test_run_environment_drops_values_that_are_not_strings(value):
    assert run_environment({"URL": value, "KEPT": "y"}) == {"KEPT": "y"}  # type: ignore[dict-item]


@pytest.mark.parametrize("value", ["a\x00b", "\x00", "postgresql://u:p@h/d\x00"])
def test_run_environment_drops_values_with_nul(value):
    # A NUL cannot be in a process environment: kept, it would make the start of
    # the executor fail with ValueError instead of costing one variable.
    assert run_environment({"URL": value, "KEPT": "y"}) == {"KEPT": "y"}


def test_run_environment_logs_a_value_with_nul_by_name_only(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("control_plane_agent.runner_config"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="control_plane_agent.runner_config"):
        run_environment({"URL": "s3cr3t\x00"})

    assert "URL" in caplog.text and "NUL" in caplog.text
    assert "s3cr3t" not in caplog.text


def test_run_environment_logs_dropped_names_but_never_values(caplog, monkeypatch):
    # Alembic's fileConfig in the migration tests disables loggers that exist by then.
    monkeypatch.setattr(logging.getLogger("control_plane_agent.runner_config"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="control_plane_agent.runner_config"):
        run_environment({"ANTHROPIC_BASE_URL": "https://s3cr3t.example", "URL": "kept"})

    assert "ANTHROPIC_BASE_URL" in caplog.text
    assert "s3cr3t" not in caplog.text


# --- repeated YAML keys ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\nversion: 1\n",
        "version: 1\nservices:\n  db: {template: pg}\n  db: {template: postgres-16}\n",
        "version: 1\nservices:\n  db:\n    template: pg\n    template: postgres-16\n",
        "version: 1\nservices:\n  db: {template: pg, env: {URL: a, URL: b}}\n",
        "version: 1\nchecks:\n  - {name: t, run: a, run: b}\n",
    ],
)
def test_repeated_yaml_key_is_refused(text):
    errors = errors_of(text)

    assert len(errors) == 1
    assert errors[0].path == "$"
    assert "duplicate key" in errors[0].message
    assert "at line" in errors[0].message


def test_repeated_key_names_the_line_of_the_repeat():
    [error] = errors_of("version: 1\nservices:\n  db: {template: pg}\n  db: {template: pg}\n")
    assert "at line 4, column 3" in error.message
    assert "'db'" in error.message


@pytest.mark.parametrize(
    "text",
    [
        # The merge would bring a second `template` past the repeat check.
        "version: 1\nservices:\n  a: &a {template: pg}\n  b:\n    <<: *a\n    template: other\n",
        "version: 1\nservices:\n  a: &a {template: pg}\n  b: {<<: *a}\n",
        "version: 1\nservices:\n  a: &a {template: pg}\n  b:\n    <<: [*a]\n",
    ],
)
def test_yaml_merge_key_is_refused(text):
    errors = errors_of(text)

    assert len(errors) == 1
    assert errors[0].path == "$"
    assert "merge keys (<<) are not supported" in errors[0].message


def test_same_key_in_different_mappings_is_not_a_repeat():
    config = parse_runner_config(
        "version: 1\nservices:\n"
        "  a: {template: pg, env: {A_URL: '{host}'}}\n"
        "  b: {template: pg, env: {B_URL: '{host}'}}\n"
    )
    assert set(config.services) == {"a", "b"}


# --- ServiceSpec checks itself --------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "env", "problem"),
    [
        ("pg_16", {}, "is not a template name"),
        ("pg\n", {}, "is not a template name"),
        ("pg", {"URL\n": "x"}, "is not a variable name"),
        ("pg", {"LD_PRELOAD": "x"}, "is reserved (LD_*)"),
        ("pg", {"https_proxy": "x"}, "is reserved (*_PROXY)"),
        ("pg", {"URL": "{name}"}, "unknown placeholder"),
        ("pg", {"URL": "{user"}, "literal braces"),
        ("pg", {"PORT": 5432}, "is not a string"),
    ],
)
def test_service_spec_refuses_what_the_parser_would(template, env, problem):
    with pytest.raises(ValueError, match="invalid service") as caught:
        ServiceSpec(template=template, env=env)
    assert problem in str(caught.value)


def test_service_spec_env_is_a_read_only_copy():
    source = {"URL": "{host}"}
    spec = ServiceSpec(template="pg", env=source)

    source["URL"] = "{name}"
    source["LD_PRELOAD"] = "x"

    assert dict(spec.env) == {"URL": "{host}"}
    with pytest.raises(TypeError):
        spec.env["URL"] = "{name}"  # type: ignore[index]


def test_parsed_service_env_is_read_only():
    config = parse_runner_config(VALID)
    with pytest.raises(TypeError):
        config.services["db"].env["LD_PRELOAD"] = "x"  # type: ignore[index]


@pytest.mark.parametrize("env", [None, ["URL"], "URL"])
def test_service_spec_refuses_env_that_is_not_a_mapping(env):
    with pytest.raises(ValueError, match="env is not a mapping"):
        ServiceSpec(template="pg", env=env)  # type: ignore[arg-type]


def test_service_spec_accepts_a_valid_entry():
    spec = ServiceSpec(template="postgres-16", env={"URL": "{host}:{port}"})
    assert spec.render_env(CREDENTIALS) == {"URL": "svc-db-1:5432"}


# --- checking a file from the command line --------------------------------------------------


def run_check(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.parametrize("how", ["module", "script"])
def test_command_line_is_silent_on_a_valid_file(tmp_path, how):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "runner.yaml").write_text(VALID)
    target = ["-m", "control_plane_agent.runner_config"] if how == "module" else [str(MODULE)]

    result = run_check(*target, cwd=tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("how", ["module", "script"])
def test_command_line_prints_errors_by_path_without_a_traceback(tmp_path, how):
    path = tmp_path / "runner.yaml"
    path.write_text("version: 1\nservices:\n  db_main: {env: {PATH: x}}\n")
    target = ["-m", "control_plane_agent.runner_config"] if how == "module" else [str(MODULE)]

    result = run_check(*target, str(path), cwd=tmp_path)

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert f"{path}: $.services.db_main: " in result.stderr
    assert f"{path}: $.services.db_main.template: required field is missing" in result.stderr


def test_command_line_on_a_missing_file_says_so(tmp_path):
    result = run_check(str(MODULE), "absent.yaml", cwd=tmp_path)

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "absent.yaml: cannot read" in result.stderr


def test_services_are_not_requested_for_an_empty_services():
    # The node refuses a request without services; the daemon sends none.
    assert dict(parse_runner_config("version: 1\nservices: {}\n").services) == {}


# --- a wrong name is explained by its rule, not by a regular expression (review of U003) -----


@pytest.mark.parametrize(
    ("text", "path", "rule"),
    [
        ("version: 1\nneighbours: [Auth]\n", "$.neighbours[0]", "repositoryKey"),
        ("version: 1\nservices:\n  db_main: {template: pg}\n", "$.services.db_main", "serviceName"),
        (
            "version: 1\nservices:\n  db: {template: pg_16}\n",
            "$.services.db.template",
            "templateName",
        ),
        (
            "version: 1\nchecks:\n  - {name: Lint, run: make lint}\n",
            "$.checks[0].name",
            "checkName",
        ),
        (
            "version: 1\nservices:\n  db: {template: pg, env: {1URL: x}}\n",
            "$.services.db.env.1URL",
            "envName",
        ),
    ],
)
def test_wrong_name_is_explained_by_its_rule(text, path, rule):
    [error] = errors_of(text)

    assert error.path == path
    assert error.message.endswith(f"is not {NAME_RULES[rule]}")
    assert "(?!" not in error.message and "^[" not in error.message


def test_every_name_pattern_has_a_rule_in_words():
    patterns = {
        name
        for name, definition in runner_config_schema()["$defs"].items()
        if "pattern" in definition and name != "reservedEnvName"
    }
    assert patterns == set(NAME_RULES)
