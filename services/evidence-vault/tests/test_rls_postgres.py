"""
Postgres row-level-security integration tests for evidence-vault (TEN-VAULT).

Mirrors dashboard_db/tests/test_rls_postgres.py exactly: runs only when
``EVIDENCE_VAULT_POSTGRES_URL`` is set (local docker Postgres, or a CI
service container), and asserts the acceptance criterion at the database
layer directly — a session opened under tenant A's GUC sees zero rows
belonging to tenant B, even when the WHERE clause explicitly names tenant B.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from opencomplai_evidence_vault.badges import BadgeDB
from opencomplai_evidence_vault.bias_alerts import BiasAlertDB
from opencomplai_evidence_vault.controls import ControlInstanceDB, ManifestFingerprintDB
from opencomplai_evidence_vault.hitl import AcceptedOverrideDB, ReviewItemDB
from opencomplai_evidence_vault.models import LedgerEventDB
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _service_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def postgres_url() -> str | None:
    return os.environ.get("EVIDENCE_VAULT_POSTGRES_URL")


@pytest_asyncio.fixture
async def pg_session_factory(postgres_url: str | None) -> Iterator[async_sessionmaker]:
    if postgres_url is None:
        pytest.skip("EVIDENCE_VAULT_POSTGRES_URL not set; skipping Postgres RLS tests")

    sync_url = postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    cfg = Config(str(_service_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sync_url)
    # Reset to a clean slate, then run every migration including 0004's RLS setup.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _tenant_session(factory: async_sessionmaker, tenant_id: str) -> AsyncSession:
    session = factory()
    await session.execute(text("SET ROLE evidence_vault_app"))
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
    )
    return session


async def test_cross_tenant_ledger_event_read_returns_zero_rows(pg_session_factory):
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        LedgerEventDB(
            event_id="evt-a",
            tenant_id="tenant-a",
            ts=datetime.now(UTC),
            event_type="test",
            payload_hash="sha256:" + "a" * 64,
            prev_hash="sha256:" + "0" * 64,
            seq=1,
        )
    )
    admin.add(
        LedgerEventDB(
            event_id="evt-b",
            tenant_id="tenant-b",
            ts=datetime.now(UTC),
            event_type="test",
            payload_hash="sha256:" + "b" * 64,
            prev_hash="sha256:" + "0" * 64,
            seq=2,
        )
    )
    await admin.commit()
    await admin.close()

    session = await _tenant_session(pg_session_factory, "tenant-a")
    try:
        rows = (
            await session.execute(
                select(LedgerEventDB).where(LedgerEventDB.tenant_id == "tenant-b")
            )
        ).scalars().all()
        assert list(rows) == []

        own_rows = (
            await session.execute(
                select(LedgerEventDB).where(LedgerEventDB.tenant_id == "tenant-a")
            )
        ).scalars().all()
        assert [r.event_id for r in own_rows] == ["evt-a"]
    finally:
        await session.close()


async def test_cross_tenant_badge_read_returns_zero_rows(pg_session_factory):
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        BadgeDB(
            id="badge-a",
            tenant_id="tenant-a",
            badge_id="sha256:" + "a" * 64,
            system_id="sys-a",
            bundle_checksum="chk-a",
            issued_at="2026-01-01T00:00:00+00:00",
            status_artifact_hash="sha256:" + "c" * 64,
        )
    )
    await admin.commit()
    await admin.close()

    session = await _tenant_session(pg_session_factory, "tenant-b")
    try:
        rows = (
            await session.execute(
                select(BadgeDB).where(BadgeDB.tenant_id == "tenant-a")
            )
        ).scalars().all()
        assert list(rows) == []
    finally:
        await session.close()


async def test_cross_tenant_bias_alert_read_returns_zero_rows(pg_session_factory):
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        BiasAlertDB(
            id="alert-a",
            tenant_id="tenant-a",
            alert_id="alert-a",
            severity="high",
            metric="demographic_parity",
            threshold=0.1,
            linked_event_id="evt-a",
        )
    )
    await admin.commit()
    await admin.close()

    session = await _tenant_session(pg_session_factory, "tenant-b")
    try:
        rows = (
            await session.execute(
                select(BiasAlertDB).where(BiasAlertDB.tenant_id == "tenant-a")
            )
        ).scalars().all()
        assert list(rows) == []
    finally:
        await session.close()


async def test_cross_tenant_review_item_read_returns_zero_rows(pg_session_factory):
    """PERSIST-RISK: review_items is RLS-fenced the same as the other four tables."""
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        ReviewItemDB(
            review_id="rev-a",
            tenant_id="tenant-a",
            system_id="sys-a",
            commit_ref="HEAD",
            reason="evaluator_fail",
            state="assigned",
            payload_ref="sha256:" + "a" * 64,
            context_ref="sha256:" + "b" * 64,
            idempotency_key="rev-a",
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    await admin.commit()
    await admin.close()

    session = await _tenant_session(pg_session_factory, "tenant-b")
    try:
        rows = (
            await session.execute(
                select(ReviewItemDB).where(ReviewItemDB.tenant_id == "tenant-a")
            )
        ).scalars().all()
        assert list(rows) == []
    finally:
        await session.close()


async def test_cross_tenant_accepted_override_read_returns_zero_rows(pg_session_factory):
    """PERSIST-RISK: accepted_overrides idempotency cache is RLS-fenced."""
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        AcceptedOverrideDB(
            idempotency_key="idem-a",
            tenant_id="tenant-a",
            payload_fingerprint="fp-a",
            response_json={"status": "accepted"},
        )
    )
    await admin.commit()
    await admin.close()

    session = await _tenant_session(pg_session_factory, "tenant-b")
    try:
        rows = (
            await session.execute(
                select(AcceptedOverrideDB).where(
                    AcceptedOverrideDB.tenant_id == "tenant-a"
                )
            )
        ).scalars().all()
        assert list(rows) == []
    finally:
        await session.close()


async def test_cross_tenant_control_instance_read_returns_zero_rows(pg_session_factory):
    """CTRL-STORE: control_instances is RLS-fenced the same as review_items."""
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        ControlInstanceDB(
            control_id="ctrl-a",
            tenant_id="tenant-a",
            system_id="sys-a",
            obligation_id="Art. 9",
            article_ref="Art. 9",
            state="evidence_missing",
            evidence_refs=[],
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    await admin.commit()
    await admin.close()

    session = await _tenant_session(pg_session_factory, "tenant-b")
    try:
        rows = (
            await session.execute(
                select(ControlInstanceDB).where(
                    ControlInstanceDB.tenant_id == "tenant-a"
                )
            )
        ).scalars().all()
        assert list(rows) == []
    finally:
        await session.close()


async def test_cross_tenant_manifest_fingerprint_read_returns_zero_rows(
    pg_session_factory,
):
    """CTRL-STORE: manifest_fingerprints is RLS-fenced the same as control_instances."""
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        ManifestFingerprintDB(
            tenant_id="tenant-a",
            system_id="sys-a",
            fingerprint="a" * 64,
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    await admin.commit()
    await admin.close()

    session = await _tenant_session(pg_session_factory, "tenant-b")
    try:
        rows = (
            await session.execute(
                select(ManifestFingerprintDB).where(
                    ManifestFingerprintDB.tenant_id == "tenant-a"
                )
            )
        ).scalars().all()
        assert list(rows) == []
    finally:
        await session.close()


async def test_session_without_tenant_guc_sees_zero_rows(pg_session_factory):
    """
    A session that SET ROLEs to evidence_vault_app but never calls
    set_config('app.tenant_id', ...) — i.e. a DAO-layer misuse — must see
    zero rows. RLS is the authoritative fence; this proves the
    coalesce-to-empty-string in the policy gives a benign zero result
    rather than an error or an accidental full-table read.
    """
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    admin.add(
        BadgeDB(
            id="badge-noguc",
            tenant_id="tenant-a",
            badge_id="sha256:" + "d" * 64,
            system_id="sys-noguc",
            bundle_checksum="chk-noguc",
            issued_at="2026-01-01T00:00:00+00:00",
            status_artifact_hash="sha256:" + "e" * 64,
        )
    )
    await admin.commit()
    await admin.close()

    session = pg_session_factory()
    try:
        await session.execute(text("SET ROLE evidence_vault_app"))
        count = (
            await session.execute(text("SELECT count(*) FROM compliance_badges"))
        ).scalar_one()
        assert count == 0
    finally:
        await session.close()
