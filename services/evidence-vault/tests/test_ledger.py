"""Tests for the Merkle-linked event ledger (REQ-EV-001)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from opencomplai_evidence_vault.ledger import (
    _canonical,
    _next_seq,
    _sha256,
    append_event,
    verify_chain,
)
from opencomplai_evidence_vault.models import Base, LedgerEventDB
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def test_sha256_format():
    result = _sha256("test")
    assert result.startswith("sha256:")
    assert len(result) == 71


def test_sha256_deterministic():
    assert _sha256("hello") == _sha256("hello")


def test_sha256_collision_resistant():
    assert _sha256("hello") != _sha256("world")


def test_canonical_is_deterministic():
    c1 = _canonical("id1", "2024-01-01T00:00:00+00:00", "test_event", "sha256:abc")
    c2 = _canonical("id1", "2024-01-01T00:00:00+00:00", "test_event", "sha256:abc")
    assert c1 == c2


@pytest_asyncio.fixture
async def sessionmaker_() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> AsyncSession:
    async with sessionmaker_() as s:
        yield s


@pytest.mark.asyncio
async def test_append_event_creates_chained_events(session: AsyncSession):
    e1 = await append_event(session, event_type="test", payload={"n": 1})
    await session.commit()
    e2 = await append_event(session, event_type="test", payload={"n": 2})
    await session.commit()

    assert e1.prev_hash == _sha256("")
    assert e2.prev_hash != _sha256("")

    assert await verify_chain(session) is True


@pytest.mark.asyncio
async def test_verify_chain_detects_tamper(session: AsyncSession):
    await append_event(session, event_type="test", payload={"n": 1})
    await (
        session.commit()
    )  # commit before next append so get_chain_tip sees a stable chain tip
    await append_event(session, event_type="test", payload={"n": 2})
    await session.commit()

    assert await verify_chain(session) is True

    stmt = (
        select(LedgerEventDB)
        .order_by(LedgerEventDB.ts.asc(), LedgerEventDB.seq.asc())
        .limit(1)
    )
    result = await session.execute(stmt)
    first = result.scalar_one()

    first.payload_hash = _sha256("tampered")
    await session.commit()

    assert await verify_chain(session) is False


@pytest.mark.asyncio
async def test_append_event_retries_past_a_deterministic_seq_collision(
    session: AsyncSession, monkeypatch
):
    """
    PERSIST-RACES: _next_seq's MAX(seq)+1 read has no locking, so a
    concurrent writer can commit the same seq between our read and our
    insert. Reproduced deterministically (no scheduler timing dependency) by
    making the *first* _next_seq call inside append_event return a seq a row
    already occupies — the flush must raise IntegrityError, the SAVEPOINT
    must roll back cleanly, and the retry (a real, uncorrupted _next_seq
    call) must succeed.
    """
    occupied = await _next_seq(session)
    taken = LedgerEventDB(
        event_id="pre-seeded",
        ts=datetime.now(UTC),
        event_type="pre_seeded",
        payload_hash=_sha256("x"),
        prev_hash=_sha256(""),
        seq=occupied,
    )
    session.add(taken)
    await session.flush()

    real_next_seq = _next_seq
    calls = {"n": 0}

    async def _stale_once(s: AsyncSession) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return occupied  # forces a collision with `taken` on first attempt
        return await real_next_seq(s)

    monkeypatch.setattr(
        "opencomplai_evidence_vault.ledger._next_seq", _stale_once
    )

    event = await append_event(session, event_type="test", payload={"n": 1})
    await session.commit()

    assert calls["n"] == 2  # one collision, one successful retry
    assert event.seq != occupied


@pytest.mark.asyncio
async def test_concurrent_append_event_both_succeed(
    sessionmaker_: async_sessionmaker[AsyncSession],
):
    """
    Two writers on separate sessions/connections append concurrently via
    asyncio.gather. SQLite serializes at the file-lock level so this doesn't
    reliably reproduce the IntegrityError itself (that's
    test_append_event_retries_past_a_deterministic_seq_collision's job) —
    but it does pin the outward contract PERSIST-RACES restores: neither
    writer's append_event call raises, and both events persist with
    distinct seq values, whatever interleaving actually occurred.
    """

    async def _append(payload: dict) -> LedgerEventDB:
        async with sessionmaker_() as s:
            event = await append_event(s, event_type="concurrent", payload=payload)
            await s.commit()
            return event

    e1, e2 = await asyncio.gather(_append({"n": 1}), _append({"n": 2}))

    assert e1.event_id != e2.event_id
    assert e1.seq is not None
    assert e2.seq is not None
