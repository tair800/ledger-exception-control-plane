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

# The entrypoint applies migrations, optionally bootstraps the demonstration, then execs uvicorn on
# the platform's `$PORT`. Marked executable here rather than relying on the checkout's mode bit
# surviving a clone on a filesystem that does not carry one — Windows does not.
#
# Copied before the user switch so the `chown` below covers it.
COPY --chmod=0755 deployment/entrypoint.sh /app/entrypoint.sh

# Run unprivileged. Nothing in the image needs to write outside /tmp.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# No secrets are baked in. Configuration arrives through LECP_* environment variables.
#
# ENTRYPOINT rather than CMD, so `docker run <image> <args>` cannot accidentally replace the
# migration step with a bare shell command — the schema must come up before the app serves, and
# that ordering should not be one flag away from being skipped.
ENTRYPOINT ["/app/entrypoint.sh"]
