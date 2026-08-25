"""Upgrade ledger chains to the v2 hash format and lock down ledger_events.

Issue #47: the v1 per-event hash covered only {event_id, ts, event_type,
payload_hash}, so a chain tip committed to a single event rather than to the
chain prefix.  An edit to event K could be concealed by patching only event
K+1's stored prev_hash; verify_chain and every dossier/badge anchor derived
from the root would keep validating.

v2 adds the event's own prev_hash and a format-version marker to the hashed
preimage (see ledger._canonical), so each hash transitively commits to the
whole prefix.  This migration rewrites every stored chain from v1 to v2 —
walking each tenant's events in seq order and recomputing the rolling
prev_hash values — so existing ledgers are explicitly migrated rather than
silently failing the new verification.

Note: rolling tips change value under v2, so dossiers generated before this
migration will no longer find their recorded ledger_root_hash in
/v1/evidence/ledger-history-tips and must be re-anchored (re-generated).

Timestamp format: the hashed preimage's `ts` field is `ts.isoformat()` with
any timezone-aware value normalized to UTC first (see `_hash_ts` below) —
never the raw driver rendering. psycopg2 (this migration's sync driver) and
asyncpg (the runtime driver) render the same timestamptz instant differently
whenever the Postgres session's TimeZone isn't UTC, so hashing the raw
rendering here would produce prev_hash values the runtime could never
reproduce, and every migrated chain would fail verify_chain. Naive
datetimes (SQLite) are hashed as-is with no conversion.

Also revokes UPDATE/DELETE/TRUNCATE on ledger_events from the request-facing
evidence_vault_app role (Postgres only): the application only ever INSERTs
and SELECTs ledger rows, and TRUNCATE in particular bypasses RLS.  0004/0005/
0007's blanket GRANT ALL left the append-only table rewritable by the role
every request runs under.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Self-contained copies of the hash helpers: a migration must reproduce the
# chain formats as they existed at this revision, independent of how
# opencomplai_evidence_vault.ledger evolves later.

_ledger_events = sa.table(
    "ledger_events",
    sa.column("event_id", sa.String(36)),
    sa.column("tenant_id", sa.String(128)),
    sa.column("ts", sa.DateTime(timezone=True)),
    sa.column("event_type", sa.String(128)),
    sa.column("payload_hash", sa.String(71)),
    sa.column("prev_hash", sa.String(71)),
    sa.column("seq", sa.BigInteger()),
)


def _sha256(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _hash_ts(ts: datetime) -> str:
    """
    Driver-independent isoformat string for hashing (self-contained copy of
    opencomplai_evidence_vault.ledger._hash_ts — migrations must not import
    app code).

    This migration runs under alembic's sync driver (psycopg2 on Postgres),
    which renders a timestamptz value in whatever TimeZone the *session* is
    set to. The runtime (asyncpg) always renders timestamptz in UTC. On a
    non-UTC-configured Postgres server the same instant then isoformats
    differently depending on which driver read it, so a chain rewritten here
    with a bare `ts.isoformat()` would hash different bytes than the
    runtime's ledger.event_hash ever could reproduce for that row, making
    every migrated chain fail verify_chain. Normalizing aware datetimes to
    UTC first keeps the hashed string identical regardless of driver or
    server TimeZone. Naive datetimes (SQLite) are hashed as-is — a naive
    value has no defined zone to convert from.
    """
    if ts.tzinfo is None:
        return ts.isoformat()
    return ts.astimezone(UTC).isoformat()


def _canonical_v1(event_id, ts_iso, event_type, payload_hash) -> str:
    return json.dumps(
        {
            "event_id": event_id,
            "ts": ts_iso,
            "event_type": event_type,
            "payload_hash": payload_hash,
        },
        sort_keys=True,
    )


def _canonical_v2(event_id, ts_iso, event_type, payload_hash, prev_hash) -> str:
    return json.dumps(
        {
            "event_id": event_id,
            "ts": ts_iso,
            "event_type": event_type,
            "payload_hash": payload_hash,
            "prev_hash": prev_hash,
            "v": 2,
        },
        sort_keys=True,
    )


def _rewrite_chains(hash_event) -> None:
    """
    Rewrite every tenant's chain: walk events in seq order, re-link each
    event to the rolling tip produced by `hash_event(row, new_prev)`.

    Ordering by seq (not ts) matches ledger.py's get_chain_tip/verify_chain/
    compute_history_tips: ts is captured once in append_event before its
    retry loop, so under concurrent writers the loser of a seq race can
    retry, land a *higher* seq, and still carry an *earlier* ts than the
    winner. A DB migrated with a ts-ordered rewrite could therefore produce
    a v2 chain that the (correctly) seq-ordered verify_chain rejects.
    (tenant_id, seq) is unique, so seq alone is already a total order.

    On Postgres this must run as the BYPASSRLS evidence_vault_admin role:
    0004 applies FORCE ROW LEVEL SECURITY to ledger_events, so even the
    table owner would otherwise see zero rows (no app.tenant_id GUC set)
    and the rewrite would silently no-op.
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET ROLE evidence_vault_admin")
    rows = bind.execute(
        sa.select(
            _ledger_events.c.event_id,
            _ledger_events.c.tenant_id,
            _ledger_events.c.ts,
            _ledger_events.c.event_type,
            _ledger_events.c.payload_hash,
            _ledger_events.c.prev_hash,
        ).order_by(
            _ledger_events.c.tenant_id,
            _ledger_events.c.seq.asc(),
        )
    ).fetchall()

    genesis = _sha256("")
    running: dict[str, str] = {}
    updates: list[dict[str, str]] = []
    for row in rows:
        new_prev = running.get(row.tenant_id, genesis)
        if row.prev_hash != new_prev:
            updates.append({"target_event_id": row.event_id, "new_prev_hash": new_prev})
        running[row.tenant_id] = hash_event(row, new_prev)

    if updates:
        # Effectively every non-genesis row changes (the v1/v2 preimages
        # differ structurally, so the rolling chain diverges after each
        # tenant's first event), and this runs inside the entrypoint's
        # blocking `alembic upgrade head` — one executemany instead of one
        # round trip per row keeps a large ledger's migration from
        # stretching deployment downtime. Bind-param names deliberately
        # differ from the column names: SQLAlchemy auto-generates a
        # bindparam per .values() column, and reusing "prev_hash" or
        # "event_id" would collide with the executemany params.
        stmt = (
            sa.update(_ledger_events)
            .where(_ledger_events.c.event_id == sa.bindparam("target_event_id"))
            .values(prev_hash=sa.bindparam("new_prev_hash"))
        )
        bind.execute(stmt, updates)

    if bind.dialect.name == "postgresql":
        op.execute("RESET ROLE")


def upgrade() -> None:
    _rewrite_chains(
        lambda row, new_prev: _sha256(
            _canonical_v2(
                row.event_id,
                _hash_ts(row.ts),
                row.event_type,
                row.payload_hash,
                new_prev,
            )
        )
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # NOTE for future migrations: a later "GRANT ALL ON ALL TABLES IN
        # SCHEMA public TO ..., evidence_vault_app" (the 0004/0005/0007
        # idiom) targets every existing table and would silently re-grant
        # UPDATE/DELETE/TRUNCATE here, reopening the tamper vector this
        # revoke closes. Use migrations/_privileges.py's
        # grant_all_tables_preserving_ledger_lockdown instead of the raw
        # statement — tests/test_migration_privilege_guard.py enforces this.
        op.execute(
            "REVOKE UPDATE, DELETE, TRUNCATE ON ledger_events FROM evidence_vault_app"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "GRANT UPDATE, DELETE, TRUNCATE ON ledger_events TO evidence_vault_app"
        )

    # v1 per-event hashes do not depend on prev_hash, so the rolling tip is
    # simply the hash of each event's own fields.
    _rewrite_chains(
        lambda row, new_prev: _sha256(
            _canonical_v1(
                row.event_id,
                _hash_ts(row.ts),
                row.event_type,
                row.payload_hash,
            )
        )
    )
