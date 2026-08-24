"""
Postgres row-level-security integration tests for evidence-vault (TEN-VAULT).

Mirrors dashboard_db/tests/test_rls_postgres.py exactly: runs only when
``EVIDENCE_VAULT_POSTGRES_URL`` is set (local docker Postgres, or a CI
service container), and asserts the acceptance criterion at the database
layer directly — a session opened under tenant A's GUC sees zero rows
belonging to tenant B, even when the WHERE clause explicitly names tenant B.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from asyncpg.exceptions import InsufficientPrivilegeError
from opencomplai_evidence_vault import ledger as ledger_module
from opencomplai_evidence_vault.badges import BadgeDB
from opencomplai_evidence_vault.bias_alerts import BiasAlertDB
from opencomplai_evidence_vault.controls import ControlInstanceDB, ManifestFingerprintDB
from opencomplai_evidence_vault.hitl import AcceptedOverrideDB, ReviewItemDB
from opencomplai_evidence_vault.ledger import append_event, verify_chain
from opencomplai_evidence_vault.models import LedgerEventDB
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
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
            (
                await session.execute(
                    select(LedgerEventDB).where(LedgerEventDB.tenant_id == "tenant-b")
                )
            )
            .scalars()
            .all()
        )
        assert list(rows) == []

        own_rows = (
            (
                await session.execute(
                    select(LedgerEventDB).where(LedgerEventDB.tenant_id == "tenant-a")
                )
            )
            .scalars()
            .all()
        )
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
            (
                await session.execute(
                    select(BadgeDB).where(BadgeDB.tenant_id == "tenant-a")
                )
            )
            .scalars()
            .all()
        )
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
            (
                await session.execute(
                    select(BiasAlertDB).where(BiasAlertDB.tenant_id == "tenant-a")
                )
            )
            .scalars()
            .all()
        )
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
            (
                await session.execute(
                    select(ReviewItemDB).where(ReviewItemDB.tenant_id == "tenant-a")
                )
            )
            .scalars()
            .all()
        )
        assert list(rows) == []
    finally:
        await session.close()


async def test_cross_tenant_accepted_override_read_returns_zero_rows(
    pg_session_factory,
):
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
            (
                await session.execute(
                    select(AcceptedOverrideDB).where(
                        AcceptedOverrideDB.tenant_id == "tenant-a"
                    )
                )
            )
            .scalars()
            .all()
        )
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
            (
                await session.execute(
                    select(ControlInstanceDB).where(
                        ControlInstanceDB.tenant_id == "tenant-a"
                    )
                )
            )
            .scalars()
            .all()
        )
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
            (
                await session.execute(
                    select(ManifestFingerprintDB).where(
                        ManifestFingerprintDB.tenant_id == "tenant-a"
                    )
                )
            )
            .scalars()
            .all()
        )
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


async def test_second_tenant_can_append_under_rls(pg_session_factory):
    """
    Issue #46 regression: _next_seq's MAX(seq) was scoped to whatever the
    calling session can see, but the old ix_ledger_events_seq unique index
    enforced uniqueness *globally*. Under RLS, tenant-b's session never sees
    tenant-a's rows, so it kept computing seq=1 even after tenant-a had
    already taken seq=1 at the table level — every retry collided and
    tenant-b's append failed after exhausting _MAX_SEQ_RETRIES. The fix
    (migration 0008's uq_ledger_events_tenant_seq) scopes uniqueness to
    (tenant_id, seq), so a second tenant appending after the first must
    succeed.
    """
    # set_config(..., true) scopes app.tenant_id to the current transaction
    # only (like SET LOCAL) — it does not survive a commit. Each append+commit
    # unit therefore needs its own freshly-GUC'd session, exactly as a real
    # request handler would open one per request.
    for n in range(3):
        session_a = await _tenant_session(pg_session_factory, "tenant-a")
        try:
            await append_event(
                session_a, event_type="test", payload={"n": n}, tenant_id="tenant-a"
            )
            await session_a.commit()
        finally:
            await session_a.close()

    session_b = await _tenant_session(pg_session_factory, "tenant-b")
    try:
        # Before the fix, this raised IntegrityError after 5 retries.
        await append_event(
            session_b, event_type="test", payload={"n": 0}, tenant_id="tenant-b"
        )
        await session_b.commit()

        assert await verify_chain(session_b, tenant_id="tenant-b") is True
    finally:
        await session_b.close()

    session_a_check = await _tenant_session(pg_session_factory, "tenant-a")
    try:
        assert await verify_chain(session_a_check, tenant_id="tenant-a") is True
    finally:
        await session_a_check.close()

    # Cleanup: tenant-a and tenant-b intentionally hold colliding seq values
    # (that's the whole point of the fix), which migration 0008's downgrade()
    # cannot re-index (see its docstring) since it briefly reinstates a
    # globally-unique index on seq. Leaving this data behind would break the
    # next test's fixture, which downgrades to "base" before every test.
    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    await admin.execute(
        text("DELETE FROM ledger_events WHERE tenant_id IN ('tenant-a', 'tenant-b')")
    )
    await admin.commit()
    await admin.close()


async def test_concurrent_same_tenant_appends_keep_chain_valid(pg_session_factory):
    """
    Issue #47 regression (retry path): before the fix, prev_hash was read
    once before append_event's retry loop, so a retried insert (after losing
    the (tenant_id, seq) race to a concurrent writer) re-linked to a stale
    tip instead of the winner's — the chain looked fine to a naive glance
    but permanently failed verify_chain. Two separate app-role sessions for
    the same tenant appending concurrently must both succeed AND leave a
    valid chain.
    """
    tenant_id = "tenant-concurrent"

    async def _append(payload: dict) -> LedgerEventDB:
        session = await _tenant_session(pg_session_factory, tenant_id)
        try:
            event = await append_event(
                session, event_type="concurrent", payload=payload, tenant_id=tenant_id
            )
            await session.commit()
            return event
        finally:
            await session.close()

    e1, e2 = await asyncio.gather(_append({"n": 1}), _append({"n": 2}))

    assert e1.event_id != e2.event_id
    assert {e1.seq, e2.seq} == {1, 2}

    check_session = await _tenant_session(pg_session_factory, tenant_id)
    try:
        assert await verify_chain(check_session, tenant_id=tenant_id) is True
    finally:
        await check_session.close()


async def test_append_event_reads_seq_before_tip_survives_interleaved_commit(
    pg_session_factory, monkeypatch
):
    """
    append_event must read seq (_next_seq) BEFORE it reads the chain tip
    (get_chain_tip). Under Postgres READ COMMITTED, each statement takes its
    own fresh snapshot, so if the tip were read first (the pre-fix order), a
    concurrent same-tenant commit landing between the two reads would hand
    this writer a *fresh*, non-colliding seq (its own MAX(seq) read happens
    after the rival's commit) paired with the *stale* tip it already read
    (before the rival's commit) — the insert succeeds, no IntegrityError
    ever fires to trigger a retry, and the chain is permanently broken.

    This test forces exactly that interleaving deterministically: it wraps
    get_chain_tip so that its first invocation during the outer append_event
    call captures the real (pre-interleaving) tip, THEN commits a rival
    event for the same tenant through a second app-role session, THEN
    returns the already-captured value — i.e. the value get_chain_tip's own
    snapshot genuinely saw before the rival committed. This is a faithful
    simulation of "the rival's commit lands after this SELECT's snapshot was
    taken" (real READ COMMITTED semantics), not a fabricated value.

    With the correct (seq-before-tip) order, seq is already captured by the
    time this fires, so the rival's commit makes seq collide with the
    rival's own seq -> append_event's existing retry recomputes both values
    fresh and the chain stays valid. Confirmed to fail (produce an invalid
    chain) under the old (tip-before-seq) order — see the finding/PR
    description for the red-run evidence.
    """
    tenant_id = "tenant-seq-before-tip"
    real_get_chain_tip = ledger_module.get_chain_tip
    triggered = False

    async def _wrapped_get_chain_tip(session, tenant_id=None):
        nonlocal triggered
        real_value = await real_get_chain_tip(session, tenant_id=tenant_id)
        if not triggered:
            triggered = True
            rival_session = await _tenant_session(pg_session_factory, tenant_id)
            try:
                await append_event(
                    rival_session,
                    event_type="rival",
                    payload={"rival": True},
                    tenant_id=tenant_id,
                )
                await rival_session.commit()
            finally:
                await rival_session.close()
        return real_value

    monkeypatch.setattr(ledger_module, "get_chain_tip", _wrapped_get_chain_tip)

    outer_session = await _tenant_session(pg_session_factory, tenant_id)
    try:
        outer_event = await append_event(
            outer_session,
            event_type="outer",
            payload={"outer": True},
            tenant_id=tenant_id,
        )
        await outer_session.commit()
    finally:
        await outer_session.close()

    assert triggered, "the rival never fired — the test setup didn't exercise the race"

    check_session = await _tenant_session(pg_session_factory, tenant_id)
    try:
        rows = (
            (
                await check_session.execute(
                    select(LedgerEventDB).where(LedgerEventDB.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert len({r.seq for r in rows}) == 2
        assert outer_event.seq in {r.seq for r in rows}

        assert await verify_chain(check_session, tenant_id=tenant_id) is True
    finally:
        await check_session.close()


async def test_app_role_cannot_rewrite_ledger(pg_session_factory):
    """
    Pins migration 0009's REVOKE UPDATE, DELETE, TRUNCATE ON ledger_events
    FROM evidence_vault_app: the request-facing role only ever INSERTs and
    SELECTs ledger rows, so a request handler compromised into issuing an
    UPDATE/DELETE against the append-only ledger must fail at the privilege
    layer, not merely be inconvenienced by RLS. Uses a fresh session per
    attempt because a failed statement poisons the enclosing transaction.
    """
    tenant_id = "tenant-lockdown"

    setup_session = await _tenant_session(pg_session_factory, tenant_id)
    try:
        await append_event(
            setup_session, event_type="test", payload={"n": 1}, tenant_id=tenant_id
        )
        await setup_session.commit()
    finally:
        await setup_session.close()

    update_session = await _tenant_session(pg_session_factory, tenant_id)
    try:
        with pytest.raises(ProgrammingError) as update_exc:
            await update_session.execute(
                text(
                    "UPDATE ledger_events SET event_type = 'rewritten' "
                    "WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        # .orig is SQLAlchemy's DBAPI-level wrapper; the real asyncpg
        # exception is chained onto it as __cause__ (see the asyncpg
        # dialect's _handle_exception: "raise translated_error from error").
        assert isinstance(update_exc.value.orig.__cause__, InsufficientPrivilegeError)
    finally:
        await update_session.rollback()
        await update_session.close()

    delete_session = await _tenant_session(pg_session_factory, tenant_id)
    try:
        with pytest.raises(ProgrammingError) as delete_exc:
            await delete_session.execute(
                text("DELETE FROM ledger_events WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        assert isinstance(delete_exc.value.orig.__cause__, InsufficientPrivilegeError)
    finally:
        await delete_session.rollback()
        await delete_session.close()


def _v1_canonical(
    event_id: str, ts_iso: str, event_type: str, payload_hash: str
) -> str:
    """Self-contained copy of the pre-0009 (v1) canonical preimage."""
    return json.dumps(
        {
            "event_id": event_id,
            "ts": ts_iso,
            "event_type": event_type,
            "payload_hash": payload_hash,
        },
        sort_keys=True,
    )


def _v1_sha256(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


async def test_migration_0009_upgrades_v1_chains(pg_session_factory, postgres_url):
    """
    Seeds a genuine v1-format chain (as a pre-0009 deployment would have
    stored one), rolls the schema back to just before 0009, then re-applies
    head and asserts the now-v2 chain verifies under the current code. Proves
    0009's rewrite actually migrates real v1 data rather than merely being
    exercised on an empty table by the per-test downgrade/upgrade cycle.
    """
    sync_url = postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    cfg = Config(str(_service_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sync_url)

    # Roll back to just before 0009 so we can seed data the way a
    # pre-migration deployment actually stored it.
    command.downgrade(cfg, "0008")

    tenant_id = "tenant-v1-migrate"
    base_ts = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    running_prev = _v1_sha256("")
    for i in range(3):
        ts = base_ts + timedelta(seconds=i)
        event_id = f"evt-v1-{i}"
        event_type = "test"
        payload_hash = _v1_sha256(f"payload-{i}")
        rows.append(
            {
                "event_id": event_id,
                "tenant_id": tenant_id,
                "ts": ts,
                "event_type": event_type,
                "payload_hash": payload_hash,
                "prev_hash": running_prev,
                "seq": i + 1,
            }
        )
        running_prev = _v1_sha256(
            _v1_canonical(event_id, ts.isoformat(), event_type, payload_hash)
        )

    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    for row in rows:
        admin.add(LedgerEventDB(**row))
    await admin.commit()
    await admin.close()

    command.upgrade(cfg, "head")

    session = await _tenant_session(pg_session_factory, tenant_id)
    try:
        assert await verify_chain(session, tenant_id=tenant_id) is True
    finally:
        await session.close()


async def test_migration_0009_upgrades_v1_chains_under_non_utc_session_timezone(
    pg_session_factory, postgres_url
):
    """
    Timezone-divergence regression: alembic's sync driver (psycopg2) renders
    a timestamptz value in the *session's* TimeZone, while the runtime's
    asyncpg driver always renders the same value in UTC. If migration 0009
    hashed the raw psycopg2 rendering, a chain migrated on a non-UTC-
    configured Postgres server would get prev_hash values the UTC-only
    runtime could never reproduce, and every migrated chain would report
    tampering on the very next verify_chain call.

    Forces a real divergence via the PGTZ environment variable: libpq (which
    psycopg2, and therefore alembic's migration runner, is built on) reads
    PGTZ at connection time and issues the equivalent of `SET TIME ZONE` for
    that session — reproducing exactly what a non-UTC-configured Postgres
    *client* environment would do, without needing database-owner privileges
    (ALTER DATABASE ... SET timezone requires DB ownership, which this
    harness's connecting role doesn't have). evidence-vault's migrations/
    env.py opens a fresh NullPool connection per command.upgrade/downgrade
    call, so setting PGTZ only around the upgrade call scopes the effect to
    exactly the migration under test. verify_chain (which reads through
    this fixture's asyncpg-based session, always UTC per asyncpg regardless
    of PGTZ) must still report the migrated chain as valid.
    """
    sync_url = postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    cfg = Config(str(_service_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sync_url)

    command.downgrade(cfg, "0008")

    tenant_id = "tenant-v1-migrate-tz"
    base_ts = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    running_prev = _v1_sha256("")
    for i in range(3):
        ts = base_ts + timedelta(seconds=i)
        event_id = f"evt-v1-tz-{i}"
        event_type = "test"
        payload_hash = _v1_sha256(f"payload-tz-{i}")
        rows.append(
            {
                "event_id": event_id,
                "tenant_id": tenant_id,
                "ts": ts,
                "event_type": event_type,
                "payload_hash": payload_hash,
                "prev_hash": running_prev,
                "seq": i + 1,
            }
        )
        running_prev = _v1_sha256(
            _v1_canonical(event_id, ts.isoformat(), event_type, payload_hash)
        )

    admin = pg_session_factory()
    await admin.execute(text("SET ROLE evidence_vault_admin"))
    for row in rows:
        admin.add(LedgerEventDB(**row))
    await admin.commit()
    await admin.close()

    # PGTZ sets the session TimeZone libpq negotiates for any *new*
    # connection opened while it's set — exactly what alembic's
    # command.upgrade opens below (a fresh NullPool connection). Restored in
    # finally regardless of outcome so it never leaks into later tests.
    previous_pgtz = os.environ.get("PGTZ")
    os.environ["PGTZ"] = "America/New_York"
    try:
        command.upgrade(cfg, "head")

        session = await _tenant_session(pg_session_factory, tenant_id)
        try:
            assert await verify_chain(session, tenant_id=tenant_id) is True
        finally:
            await session.close()
    finally:
        if previous_pgtz is None:
            os.environ.pop("PGTZ", None)
        else:
            os.environ["PGTZ"] = previous_pgtz
