# syntax=docker/dockerfile:1

# Minimal image for the local stack. Not a deployment artifact — deployment is increment 10.1.
FROM python:3.12-slim-bookworm

# uv installs from the lockfile, so the container gets byte-identical dependencies to local.
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies are installed before the source is copied, so a source-only change does not
# invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Run unprivileged. Nothing in the image needs to write outside /tmp.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# No secrets are baked in. Configuration arrives through LECP_* environment variables.
CMD ["uvicorn", "ledger_exception_control_plane.api:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
