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

# Migrations travel with the image, because the deployment applies them as a **release command**
# rather than from the application process — a process that migrates on boot races every other
# replica for the same DDL, and on a rolling deploy the old and new schema are live at once.
#
# Their absence was found empirically, not by reading: without `alembic.ini` the release command
# fails with `No 'script_location' key found in configuration` and the very first deploy never
# releases. A migration step that cannot see its own script directory is a deployment that stops
# at the point where it would have changed the database.
COPY alembic.ini ./
COPY migrations/ ./migrations/

# The fixture corpus, so a deployed demonstration can be seeded in-container. Committed, seeded,
# and byte-identical to what the generator produces — the same artefact CI drift-checks, not a
# copy made for the image.
COPY fixtures/ ./fixtures/
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
