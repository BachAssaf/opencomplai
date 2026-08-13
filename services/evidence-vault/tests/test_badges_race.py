"""
PERSIST-RACES: badge issuance idempotent-insert race.

issue_badge's existence check and insert are not atomic — a concurrent
issuer for the same (tenant_id, badge_id) can commit its row in the window
between our existence check and our own insert. Before this fix that raised
an unhandled IntegrityError on the unique (tenant_id, badge_id) index
(migration 0004's ix_compliance_badges_tenant_badge) — a bare 500 instead of
the documented idempotent-success contract.

Reproduced deterministically (no scheduler timing dependency), mirroring
test_ledger.py's approach: the DAO's own pre-insert visibility check is
forced to report "not found" via monkeypatch, so the insert itself is what
collides with a row committed just before.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from opencomplai_evidence_vault.badges import (
    OSS_DEFAULT_TENANT_ID,
    BadgeDB,
    _BadgeBase,
    _make_badge_id,
    issue_badge,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_ARTIFACT = {"result": "pass", "pending_verifications_count": 0}


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(_BadgeBase.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s

    await engine.dispose()


@pytest.mark.asyncio
async def test_issue_badge_recovers_via_reread_on_unique_index_collision(
    session: AsyncSession, monkeypatch
):
    badge_id = _make_badge_id("sys-1", "chk-1")
    winner = BadgeDB(
        id="winner-row",
        tenant_id=OSS_DEFAULT_TENANT_ID,
        badge_id=badge_id,
        system_id="sys-1",
        bundle_checksum="chk-1",
        issued_at="2026-01-01T00:00:00+00:00",
        status_artifact_hash="sha256:whatever",
        signature=None,
    )
    session.add(winner)
    await session.commit()

    real_execute = session.execute
    calls = {"n": 0}

    async def _blind_first_check(stmt, *args, **kwargs):
        """
        Make issue_badge's pre-insert existence check miss exactly once —
        simulating that it ran in the instant before `winner` committed —
        so its subsequent insert is the one that collides with `winner`'s
        already-committed unique (tenant_id, badge_id) index entry.
        """
        calls["n"] += 1
        if calls["n"] == 1:

            class _EmptyResult:
                def scalar_one_or_none(self) -> None:
                    return None

            return _EmptyResult()
        return await real_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "execute", _blind_first_check)

    badge, created = await issue_badge(
        session=session,
        system_id="sys-1",
        bundle_checksum="chk-1",
        artifact=_ARTIFACT,
    )

    assert created is False
    assert badge.id == "winner-row"
