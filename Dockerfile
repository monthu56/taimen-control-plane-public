# The build context is the directory *above* this repository, because the
# enforcement SDK (IAM-7) lives in a sibling repository and is consumed as a
# path dependency: the platform has no internal package index yet.
#
#   docker build -f control-plane/Dockerfile -t control-plane ..
#
# docker compose already passes the right context; see docker-compose.yml.

# --- build stage --------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app/control-plane

# The sibling package has to be in place before the lockfile is resolved.
COPY platform-auth-sdk /app/platform-auth-sdk

# Dependency layer (cached until the lockfile changes). The client is a path
# dependency of this same repository, so it has to be present before resolving.
COPY control-plane/pyproject.toml control-plane/uv.lock control-plane/README.md ./
COPY control-plane/client ./client
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Application layer.
COPY control-plane/src ./src
COPY control-plane/alembic.ini ./
COPY control-plane/migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime stage -------------------------------------------------------------
FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app/control-plane
COPY --from=builder --chown=appuser:appuser /app /app
ENV PATH="/app/control-plane/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER appuser
EXPOSE 8000
CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
