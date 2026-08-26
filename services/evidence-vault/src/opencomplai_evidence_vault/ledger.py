"""
Append-only Merkle-linked event ledger.

Every event is chained to the previous event via prev_hash, forming a
tamper-evident chain. Modifying any event causes all subsequent events'
prev_hash values to become invalid (REQ-EV-001).

Chain format v2: each event's hash commits to the event *and* to its
prev_hash, so the rolling tip after event N commits to the entire chain
prefix — not just to event N. Under the previous (v1) format the tip hashed
only the event's own fields, which meant an edit to a historical event could
be concealed by patching a single successor row's prev_hash; v2 makes any
prefix edit change every subsequent hash, including the tip that dossiers
and badges anchor to. Migration 0009 rewrites existing chains from v1 to v2.
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

# Version marker mixed into every hashed preimage (domain separation). A v1
# chain (no marker, no prev_hash in the preimage) fails v2 verification
# rather than being silently reinterpreted; migration 0009 upgrades stored
# chains in place.
CHAIN_FORMAT_VERSION = 2


def _sha256(data: str) -> str:
    """Return sha256:<hex> of the UTF-8 encoded string."""
    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _hash_ts(ts: datetime) -> str:
    """
    Return the driver-independent isoformat string hashed into an event's
    preimage.

    psycopg2 (alembic's sync driver, used by migration 0009) renders a
    timestamptz column in whatever TimeZone the Postgres *session* is set
    to, while asyncpg (this module's runtime driver) always renders it in
    UTC. On a non-UTC-configured Postgres server, the same instant then
    isoformats differently depending on which driver read it — e.g.
    '2026-01-01T11:00:00+01:00' (psycopg2, session TZ=Europe/Berlin) vs.
    '2026-01-01T10:00:00+00:00' (asyncpg) — so a migration-computed hash
    could never be reproduced by the runtime, and every chain would report
    tampering. Normalizing any timezone-aware value to UTC before
    isoformat makes the hashed string identical regardless of which driver
    or server TimeZone produced the datetime.

    Naive datetimes (e.g. SQLite, which stores no tzinfo) are hashed as-is:
    astimezone() on a naive value would assume the *local* zone, which is
    both wrong and non-deterministic across machines.
    """
    if ts.tzinfo is None:
        return ts.isoformat()
    return ts.astimezone(UTC).isoformat()


def _canonical(
    event_id: str, ts: str, event_type: str, payload_hash: str, prev_hash: str
) -> str:
    """
    Produce the canonical string representation of an event for hashing.

    prev_hash is part of the preimage: because each event's prev_hash is the
    hash of its predecessor (computed the same way), every event hash
    transitively commits to the whole chain prefix.
    """
    return json.dumps(
        {
            "event_id": event_id,
            "ts": ts,
            "event_type": event_type,
            "payload_hash": payload_hash,
            "prev_hash": prev_hash,
            "v": CHAIN_FORMAT_VERSION,
        },
        sort_keys=True,
    )


def event_hash(event: LedgerEventDB) -> str:
    """Return the v2 chain hash of a stored ledger event."""
    return _sha256(
        _canonical(
            event.event_id,
            _hash_ts(event.ts),
            event.event_type,
            event.payload_hash,
            event.prev_hash,
        )
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

    Because the stored prev_hash on the latest event already commits to the
    prefix before it, the tip is computable from the latest row alone.

    Ordering: by seq alone (descending), matching verify_chain and
    compute_history_tips. seq — not ts — is the authoritative append order:
    ts is captured once in append_event *before* its retry loop, so when two
    writers race, the loser can retry, land a *higher* seq, and still carry
    an *earlier* ts than the winner. Ordering by ts (even as a tie-break
    ahead of seq) can then pick the wrong row as "latest" — reproduced via
    two genuinely concurrent Postgres writers in
    test_concurrent_same_tenant_appends_keep_chain_valid, which intermittently
    failed under ts-first ordering despite append_event's prev_hash linkage
    being correct. (tenant_id, seq) is unique, so seq alone is already a
    total order — no tie-breaker is needed.
    """
    stmt = (
        select(LedgerEventDB)
        .where(LedgerEventDB.tenant_id == tenant_id)
        .order_by(LedgerEventDB.seq.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    latest = result.scalar_one_or_none()

    if latest is None:
        return _sha256("")

    return event_hash(latest)


async def _next_seq(
    session: AsyncSession, tenant_id: str = OSS_DEFAULT_TENANT_ID
) -> int:
    """
    Return the next monotonic sequence number for a new ledger event,
    scoped to tenant_id.

    seq is tenant-partitioned and is the sole, authoritative ordering key for
    chain-tip ordering (see get_chain_tip): unlike ts, which is captured once
    in append_event before its retry loop and can end up out of order under
    concurrent writers, seq is only ever claimed via the (tenant_id, seq)
    unique constraint at insert time, so it always reflects true append
    order. Scoping the MAX to the tenant keeps the computed value consistent
    with what the constraint checks even under Postgres RLS, where this
    session only sees its own tenant's rows — a global MAX against a global
    unique index deadlocked every tenant except the one holding the
    globally-highest seq (issue #46).

    The result is MAX(seq) + 1, or 1 for the genesis event.  Concurrent
    same-tenant writers can still race between this read and the insert;
    append_event resolves that via the unique constraint plus retry.
    """
    result = await session.execute(
        select(func.max(LedgerEventDB.seq)).where(LedgerEventDB.tenant_id == tenant_id)
    )
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

    ts is captured once, before the retry loop, so it reflects when this call
    started rather than when (or in what order) it ultimately committed. The
    seq column is the authoritative ordering key for chain-tip/verify_chain
    purposes precisely because of this: under concurrent writers, the loser
    of a race can retry and land a *higher* seq while still carrying an
    *earlier* ts than the winner, so ordering by ts (even ahead of seq as a
    tie-break) can pick the wrong row as "latest". get_chain_tip, verify_chain,
    and compute_history_tips all order by seq alone.

    Both prev_hash and seq are recomputed on every retry attempt, and are
    read in that order — seq FIRST, then prev_hash — every time.  This order
    is what makes a colliding retry always pick up the *winner's* tip rather
    than a stale one, even though each read is its own statement and (under
    Postgres READ COMMITTED) gets its own fresh snapshot:

    Suppose a concurrent same-tenant commit becomes visible between our two
    reads. It must be visible to the later read (prev_hash/get_chain_tip),
    since a snapshot only ever sees more, never less, than an earlier one
    taken in the same transaction. Two cases:
      - It's also visible to the earlier read (seq/_next_seq): then both of
        our reads already account for it, and nothing is stale.
      - It's visible to prev_hash but NOT to seq: then that writer computed
        its own seq as (the same MAX(seq) our seq-read snapshot saw) + 1 —
        i.e. exactly our claimed seq. Our insert therefore collides on the
        (tenant_id, seq) unique constraint and raises IntegrityError, and the
        retry recomputes both values fresh, picking up the real winner's
        seq and tip.
    So a writer can never end up with a *non-colliding* seq (insert
    succeeds) alongside a *stale* prev_hash — the collision is guaranteed
    exactly when the tip would otherwise be stale. (The reverse order —
    prev_hash before seq, as this used to be written — breaks this: a commit
    landing between the two reads is then visible to the *later* seq read
    but not the earlier prev_hash read, so the writer gets a fresh,
    non-colliding seq with a stale prev_hash, the insert succeeds, and the
    chain silently fails verify_chain — issue #47's actual failure mode, not
    just the naive "read prev_hash once before the loop" bug it was first
    filed as.)  A SAVEPOINT keeps the retry scoped to just the insert
    attempt, so other statements already pending in the caller's transaction
    (e.g. purge_bias_data_endpoint's delete() before its audit event)
    survive the retry instead of being rolled back with it.
    """
    payload_hash = _sha256(json.dumps(payload, sort_keys=True))
    event_id = str(uuid.uuid4())
    ts = datetime.now(UTC)

    for attempt in range(_MAX_SEQ_RETRIES):
        seq = await _next_seq(session, tenant_id=tenant_id)
        prev_hash = await get_chain_tip(session, tenant_id=tenant_id)
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

    Ordering: by seq alone — see get_chain_tip's docstring for why seq, not
    ts, is the authoritative append order.
    """
    stmt = (
        select(LedgerEventDB)
        .where(LedgerEventDB.tenant_id == tenant_id)
        .order_by(LedgerEventDB.seq.asc())
    )
    result = await session.execute(stmt)
    events = result.scalars().all()

    tips: list[str] = [_sha256("")]  # genesis tip (empty ledger)

    for event in events:
        tips.append(event_hash(event))

    return tips


async def verify_chain(
    session: AsyncSession, tenant_id: str = OSS_DEFAULT_TENANT_ID
) -> bool:
    """
    Verify the integrity of the tenant's ledger chain.

    Returns True if all events form a valid Merkle chain.
    Returns False if any event's prev_hash does not match the rolling hash of
    the chain prefix before it (tamper detection — REQ-EV-001).  Because each
    event's hash commits to its own prev_hash, the rolling value at step N
    commits to every earlier event, so an edit to event K cannot be concealed
    by patching only event K+1's prev_hash.

    A chain written in the legacy v1 format (pre-migration-0009) fails this
    check rather than being silently reinterpreted; run migration 0009 to
    upgrade stored chains.

    Ordering: by seq alone — see get_chain_tip's docstring for why seq, not
    ts, is the authoritative append order.
    """
    stmt = (
        select(LedgerEventDB)
        .where(LedgerEventDB.tenant_id == tenant_id)
        .order_by(LedgerEventDB.seq.asc())
    )
    result = await session.execute(stmt)
    events = result.scalars().all()

    if not events:
        return True

    expected_prev = _sha256("")

    for event in events:
        if event.prev_hash != expected_prev:
            return False
        expected_prev = event_hash(event)

    return True
