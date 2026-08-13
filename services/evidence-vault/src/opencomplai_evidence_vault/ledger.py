"""
Append-only Merkle-linked event ledger.

Every event is chained to the previous event via prev_hash, forming a
tamper-evident chain. Modifying any event causes all subsequent events'
prev_hash values to become invalid (REQ-EV-001).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opencomplai_evidence_vault.models import OSS_DEFAULT_TENANT_ID, LedgerEventDB

_MAX_SEQ_RETRIES = 5


def _sha256(data: str) -> str:
    """Return sha256:<hex> of the UTF-8 encoded string."""
    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical(event_id: str, ts: str, event_type: str, payload_hash: str) -> str:
    """Produce the canonical string representation of an event for hashing."""
    return json.dumps(
        {
            "event_id": event_id,
            "ts": ts,
            "event_type": event_type,
            "payload_hash": payload_hash,
        },
        sort_keys=True,
    )


async def get_chain_tip(
    session: AsyncSession, tenant_id: str = OSS_DEFAULT_TENANT_ID
) -> str:
    """
    Return the prev_hash to use for the next event, scoped to tenant_id.

    Each tenant has its own independent Merkle chain — sharing one chain
    across tenants would leak cross-tenant signal (event count/timing) via
    the chain tip a dossier anchors to, and materialise every tenant's
    events together in verify_chain/history_tips. If the ledger is empty for
    this tenant, returns the genesis hash (sha256 of the empty string).

    Ordering: (ts ASC, seq ASC) mirrors verify_chain so that both functions
    agree on which event is "last" even when two events share the same ts value
    (sub-second resolution on SQLite/Windows).  We pick the last row by ordering
    ascending and taking the final result via DESC on the same columns with LIMIT 1.
    """
    stmt = (
        select(LedgerEventDB)
        .where(LedgerEventDB.tenant_id == tenant_id)
        .order_by(LedgerEventDB.ts.desc(), LedgerEventDB.seq.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    latest = result.scalar_one_or_none()

    if latest is None:
        return _sha256("")

    canonical = _canonical(
        latest.event_id,
        latest.ts.isoformat(),
        latest.event_type,
        latest.payload_hash,
    )
    return _sha256(canonical)


async def _next_seq(session: AsyncSession) -> int:
    """
    Return the next monotonic sequence number for a new ledger event.

    seq remains a single global sequence across all tenants (not
    tenant-partitioned) — it exists purely as a same-`ts` tie-breaker for
    chain-tip ordering (see get_chain_tip), not as a per-tenant event
    counter, so cross-tenant contention on this value is harmless. Queries
    MAX(seq) within the current transaction so that concurrent writers do
    not collide.  The result is MAX(seq) + 1, or 1 for the genesis event.

    Note: this relies on the caller holding an exclusive write lock (via the
    surrounding transaction) — it is safe for single-writer use but would need
    a FOR UPDATE lock on multi-writer Postgres deployments.
    """
    result = await session.execute(select(func.max(LedgerEventDB.seq)))
    current_max = result.scalar_one_or_none()
    return 1 if current_max is None else current_max + 1


async def append_event(
    session: AsyncSession,
    event_type: str,
    payload: dict,
    signer_id: str | None = None,
    tenant_id: str = OSS_DEFAULT_TENANT_ID,
) -> LedgerEventDB:
    """
    Append a new event to the tenant's ledger chain.

    Computes payload_hash, prev_hash (scoped to tenant_id's chain), and a
    monotonic seq number, persists the event, and returns it.  The caller is
    responsible for committing the session.

    The seq column is the authoritative tie-breaker for events that share the
    same ts value (which can happen on platforms where DateTime has coarser-than-
    microsecond resolution, e.g. SQLite on Windows).

    Retries the seq assignment (and only the seq assignment, via a
    SAVEPOINT) up to `_MAX_SEQ_RETRIES` times if a concurrent writer commits
    the same seq first — `_next_seq`'s MAX(seq)+1 read has a TOCTOU window
    between two transactions with no locking, and `seq` carries a unique
    constraint, so a genuine collision raises IntegrityError rather than
    silently overwriting. A SAVEPOINT keeps this scoped to just the insert
    attempt, so other statements already pending in the caller's transaction
    (e.g. purge_bias_data_endpoint's delete() before its audit event) survive
    the retry instead of being rolled back with it.
    """
    payload_hash = _sha256(json.dumps(payload, sort_keys=True))
    prev_hash = await get_chain_tip(session, tenant_id=tenant_id)
    event_id = str(uuid.uuid4())
    ts = datetime.now(UTC)

    for attempt in range(_MAX_SEQ_RETRIES):
        seq = await _next_seq(session)
        event = LedgerEventDB(
            event_id=event_id,
            tenant_id=tenant_id,
            ts=ts,
            event_type=event_type,
            payload_hash=payload_hash,
            prev_hash=prev_hash,
            seq=seq,
            signer_id=signer_id,
        )
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            if attempt == _MAX_SEQ_RETRIES - 1:
                raise
            continue
        return event

    raise AssertionError("unreachable")  # pragma: no cover


async def compute_history_tips(
    session: AsyncSession, tenant_id: str = OSS_DEFAULT_TENANT_ID
) -> list[str]:
    """
    Walk the tenant's ledger chain in order and return the rolling Merkle tip
    after each event.

    The first element is the genesis hash (sha256 of ""), the Nth element is
    the running tip after the Nth event has been applied.  An Annex IV dossier
    anchors to the tip at the moment it was generated; calling this function and
    checking whether the dossier's ledger_root_hash appears in the returned list
    confirms the dossier was issued against an unmodified version of the chain.

    For efficiency this is O(N) over the chain length.  For large ledgers,
    consider a dedicated /v1/evidence/ledger-history-tips endpoint that streams
    or paginates rather than materialising the full list in memory.
    """
    stmt = (
        select(LedgerEventDB)
        .where(LedgerEventDB.tenant_id == tenant_id)
        .order_by(LedgerEventDB.ts.asc(), LedgerEventDB.seq.asc())
    )
    result = await session.execute(stmt)
    events = result.scalars().all()

    tips: list[str] = [_sha256("")]  # genesis tip (empty ledger)

    for event in events:
        canonical = _canonical(
            event.event_id,
            event.ts.isoformat(),
            event.event_type,
            event.payload_hash,
        )
        tips.append(_sha256(canonical))

    return tips


async def verify_chain(
    session: AsyncSession, tenant_id: str = OSS_DEFAULT_TENANT_ID
) -> bool:
    """
    Verify the integrity of the tenant's ledger chain.

    Returns True if all events form a valid Merkle chain.
    Returns False if any event's prev_hash does not match the hash of the
    preceding event (tamper detection — REQ-EV-001).
    """
    stmt = (
        select(LedgerEventDB)
        .where(LedgerEventDB.tenant_id == tenant_id)
        .order_by(LedgerEventDB.ts.asc(), LedgerEventDB.seq.asc())
    )
    result = await session.execute(stmt)
    events = result.scalars().all()

    if not events:
        return True

    expected_prev = _sha256("")

    for event in events:
        if event.prev_hash != expected_prev:
            return False
        canonical = _canonical(
            event.event_id,
            event.ts.isoformat(),
            event.event_type,
            event.payload_hash,
        )
        expected_prev = _sha256(canonical)

    return True
