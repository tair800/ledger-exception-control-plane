#!/bin/sh
# Container entrypoint: bring the schema up, optionally seed the demonstration, then serve.
#
# **Why migrations run here and not as a release command.** `deployment/fly.*.toml` uses
# `release_command = "alembic upgrade head"`, and the Dockerfile says at length why that is the
# right shape: a process that migrates on boot races every other replica for the same DDL, and on
# a rolling deploy the old and new schema are live at once.
#
# Render's free instance type has no release or pre-deploy hook — that is a paid feature — so the
# choice on the free tier is migrate-on-boot or migrate-by-hand. Migrate-on-boot is taken here, and
# the reasoning the Dockerfile gives does not apply to this deployment for a specific reason rather
# than a hopeful one: **the free tier runs exactly one instance**, with no horizontal scaling and no
# rolling deploy. There is no second replica to race. That is a property of the plan, so it is
# recorded in `docs/deployment.md` as a limitation that returns the moment the service is scaled.
#
# `set -e` matters more than usual: a failed migration must stop the container rather than let
# uvicorn come up against a schema the code does not expect. Render restarts it, the deploy stays
# unhealthy, and that is the correct outcome.
set -eu

echo "entrypoint: applying migrations"
alembic upgrade head

# The demonstration seeds itself once. `bootstrap` is idempotent by *inspection* — it seeds only an
# empty database — rather than by reset, because a free container cold-starts often and a
# reset-on-boot would discard whatever a visitor had just approved.
#
# Gated on demo mode so this can never run against an instance that is not a demonstration. The
# seeder additionally refuses any database whose name is not `lecp_(test|demo|fixtures)`, which is
# the guard that makes a misconfigured DSN a refusal instead of invented transactions.
if [ "${LECP_DEMO_MODE:-false}" = "true" ]; then
  echo "entrypoint: demo mode is on, bootstrapping the demonstration if absent"
  python -m ledger_exception_control_plane.demo bootstrap
fi

# `exec` so uvicorn replaces this shell and becomes the process the platform signals. Without it,
# SIGTERM reaches /bin/sh, which does not forward it, and every shutdown becomes a kill after the
# grace period — the difference between a clean drain and a severed connection mid-request.
#
# PORT is supplied by the platform. The default keeps `docker run` working unchanged locally.
echo "entrypoint: starting uvicorn on ${PORT:-8000}"
exec uvicorn ledger_exception_control_plane.api:create_app \
  --factory \
  --host 0.0.0.0 \
  --port "${PORT:-8000}"
