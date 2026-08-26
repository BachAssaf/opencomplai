#!/bin/sh
# Entrypoint for the evidence-vault container image.
#
# Runs pending Alembic migrations against DATABASE_URL before uvicorn starts
# serving. Without this, the only path to a migrated Postgres schema was
# EVIDENCE_VAULT_AUTO_MIGRATE=1 inside main.py's lifespan (opt-in, defaults
# to off) — so a plain `docker compose up` against a fresh Postgres volume
# left the evidence_vault_app role (created by migration 0004) missing, and
# every /v1/* request 500'd with 'role "evidence_vault_app" does not exist'
# while /health kept reporting ok (issue #48, finding 11).
#
# This script makes migrating the default, non-opt-in path for the
# container: it always runs `alembic upgrade head` and refuses to start
# uvicorn (exit 1) if that fails, instead of serving traffic against an
# unmigrated or partially-migrated database.
#
# Set EVIDENCE_VAULT_SKIP_MIGRATIONS=1 to opt out — e.g. a separate
# migration job/step already ran `alembic upgrade head` for this database,
# or a deployment topology where this container must never touch schema.
# See infra/migrations/README.md for the manual invocation this mirrors.
set -eu

SERVICE_ROOT="${EVIDENCE_VAULT_SERVICE_ROOT:-/app/services/evidence-vault}"

if [ "${EVIDENCE_VAULT_SKIP_MIGRATIONS:-0}" = "1" ]; then
    echo "evidence-vault entrypoint: EVIDENCE_VAULT_SKIP_MIGRATIONS=1 — skipping alembic upgrade" >&2
elif [ -z "${DATABASE_URL:-}" ]; then
    # No DATABASE_URL: main.py falls back to its own local sqlite default
    # and creates its schema via SQLAlchemy metadata.create_all in the
    # lifespan (the dev/test path) — there is nothing for Alembic to target.
    echo "evidence-vault entrypoint: DATABASE_URL not set — skipping alembic upgrade, app will use its sqlite default" >&2
else
    echo "evidence-vault entrypoint: running 'alembic upgrade head' against DATABASE_URL" >&2
    if ! (cd "$SERVICE_ROOT" && alembic upgrade head); then
        echo "evidence-vault entrypoint: FATAL — alembic upgrade head failed; refusing to start uvicorn against an unmigrated database" >&2
        exit 1
    fi
fi

exec uvicorn opencomplai_evidence_vault.main:app --host 0.0.0.0 --port 8002
