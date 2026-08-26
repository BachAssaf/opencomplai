"""
Readiness endpoint (/ready) tests — FINDING 48.11.

main.py's get_tenant_session does `SET ROLE evidence_vault_app` on every
Postgres-backed /v1/* request, but that role only exists once migration
0004 has run. Before this fix, /health was a static liveness check that
always reported "ok", so an unmigrated deployment looked healthy while
every real request 500'd with 'role "evidence_vault_app" does not exist'.
/ready exists to catch that class of failure instead.

The Postgres-specific tests below run only when EVIDENCE_VAULT_POSTGRES_URL
is set (local docker Postgres or a CI service container), mirroring
tests/test_rls_postgres.py's own gate and fixture pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from opencomplai_evidence_vault.badges import _BadgeBase
from opencomplai_evidence_vault.bias_alerts import _Base as _BiasBase
from opencomplai_evidence_vault.main import _alembic_ini_path, create_app
from opencomplai_evidence_vault.main import app as _module_app
from opencomplai_evidence_vault.models import Base as _LedgerBase
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _service_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_alembic_ini_path_resolves_to_real_file():
    """Regression test for the _service_root() off-by-one (finding 48.11):
    it previously returned services/ instead of services/evidence-vault/,
    so _alembic_ini_path().exists() was always False and
    EVIDENCE_VAULT_AUTO_MIGRATE=1 silently never ran migrations."""
    assert _alembic_ini_path().exists()
    assert _alembic_ini_path() == _service_root() / "alembic.ini"


async def test_ready_without_initialized_state_returns_503():
    """Mirrors test_health.py's bare-`app` style: before lifespan wires up
    app.state.sessionmaker, /ready must fail closed instead of 500ing."""
    async with AsyncClient(
        transport=ASGITransport(app=_module_app), base_url="http://test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


@pytest_asyncio.fixture
async def sqlite_client(tmp_path):
    db_path = tmp_path / "test-ready-sqlite.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_LedgerBase.metadata.create_all)
        await conn.run_sync(_BiasBase.metadata.create_all)
        await conn.run_sync(_BadgeBase.metadata.create_all)

    app = create_app()
    app.state.engine = engine
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    await engine.dispose()


async def test_ready_with_initialized_sqlite_state_returns_ready(sqlite_client):
    """SQLite has no evidence_vault_app role to check — the role check is
    dialect-gated in the endpoint, matching how get_tenant_session already
    treats SQLite as a no-op for SET ROLE / RLS GUCs."""
    response = await sqlite_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.fixture(scope="session")
def postgres_url() -> str | None:
    return os.environ.get("EVIDENCE_VAULT_POSTGRES_URL")


@pytest_asyncio.fixture
async def postgres_client_unmigrated(postgres_url, monkeypatch):
    if postgres_url is None:
        pytest.skip(
            "EVIDENCE_VAULT_POSTGRES_URL not set; skipping Postgres readiness tests"
        )

    # migrations/env.py's run_migrations_online() ignores
    # cfg.set_main_option("sqlalchemy.url", ...) and re-reads DATABASE_URL
    # from the environment directly (see _get_database_url) — without this,
    # command.downgrade below would either raise (DATABASE_URL unset) or,
    # worse, silently run against whatever DATABASE_URL happens to be set to.
    sync_url = postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    monkeypatch.setenv("DATABASE_URL", sync_url)
    cfg = Config(str(_service_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.downgrade(cfg, "base")

    engine = create_async_engine(postgres_url, echo=False)
    app = create_app()
    app.state.engine = engine
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    await engine.dispose()
    # Leave the schema at head for any other Postgres test that runs after.
    command.upgrade(cfg, "head")


async def test_ready_against_unmigrated_postgres_returns_503(
    postgres_client_unmigrated,
):
    response = await postgres_client_unmigrated.get("/ready")
    assert response.status_code == 503
    # The unmigrated state can surface as either a missing role or a missing
    # table (roles are cluster-level and may survive a schema downgrade) —
    # pin the shared actionable message, not which probe fired first.
    assert "migrations have not run" in response.json()["detail"]


@pytest_asyncio.fixture
async def postgres_client_migrated(postgres_url, monkeypatch):
    if postgres_url is None:
        pytest.skip(
            "EVIDENCE_VAULT_POSTGRES_URL not set; skipping Postgres readiness tests"
        )

    # See postgres_client_unmigrated above: env.py re-reads DATABASE_URL from
    # the environment regardless of cfg.set_main_option.
    sync_url = postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    monkeypatch.setenv("DATABASE_URL", sync_url)
    cfg = Config(str(_service_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(cfg, "head")

    engine = create_async_engine(postgres_url, echo=False)
    app = create_app()
    app.state.engine = engine
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    await engine.dispose()


async def test_ready_against_migrated_postgres_returns_ready(postgres_client_migrated):
    response = await postgres_client_migrated.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
