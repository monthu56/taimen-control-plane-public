.PHONY: install lint fmt typecheck test test-db-up test-db-down run-api run-worker \
        migrate db-up compose-up compose-down check event-catalog

# The targets below are the interface: CI (.github/workflows/ci.yml) and the
# runner daemon (.agents/runner.yaml) call them instead of copying commands.
install:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check . --fix
	uv run ruff format .

typecheck:
	uv run mypy

db-up:
	docker compose up -d db

test-db-up:
	docker compose --profile test up -d db-test

test-db-down:
	docker compose --profile test rm -sf db-test

# One test run per working copy: a second `make test` exits at once instead of
# queueing behind (or deadlocking with) the first. The lock is held by fd 9,
# inherited by pytest, and released when the run ends, however it ends.
# Where flock(1) is missing (macOS) the run goes on unlocked with a warning.
# Without CP_TEST_DATABASE_URL the compose db-test is started first.
# Extra pytest arguments: make test PYTEST_ARGS="tests/unit -x".
TEST_LOCK := .pytest.lock

test:
	@exec 9>$(TEST_LOCK); \
	if ! command -v flock >/dev/null; then \
		echo "flock не найден (util-linux): запуск без блокировки $(TEST_LOCK)" >&2; \
	elif ! flock -n 9; then \
		echo "тесты уже идут: $(TEST_LOCK) занят другим make test в этой рабочей копии" >&2; \
		exit 75; \
	fi; \
	if [ -z "$$CP_TEST_DATABASE_URL" ]; then \
		docker compose --profile test up -d db-test || exit $$?; \
	fi; \
	uv run pytest $(PYTEST_ARGS)

migrate:
	uv run alembic upgrade head

# Event catalog docs from the registry in code (CP-ADR-0068); a test keeps them in step.
event-catalog:
	uv run python -m control_plane.domain.event_catalog docs/events

run-api:
	uv run uvicorn control_plane.main:app --reload --port 8000

run-worker:
	uv run python -m control_plane.worker

compose-up:
	docker compose up -d --build db api worker

compose-down:
	docker compose down

check: lint typecheck test
