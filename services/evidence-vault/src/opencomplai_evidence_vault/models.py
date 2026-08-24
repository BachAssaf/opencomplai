from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Sentinel tenant_id for OSS/self-hosted callers that carry no tenant context
# (the CLI's direct evidence-vault use, or any caller that omits X-Tenant-Id).
# Keeps a single evidence-vault schema/deployment serving both OSS installs
# and SaaS tenants — SaaS tenants never collide with this value since real
# tenant ids come from dashboard_db.tenants.id (TEN-VAULT).
OSS_DEFAULT_TENANT_ID = "oss-default"


class Base(DeclarativeBase):
    pass


class LedgerEventDB(Base):
    __tablename__ = "ledger_events"
    # seq uniqueness is scoped per tenant: each tenant has an independent
    # chain, and _next_seq computes MAX(seq)+1 from the rows the session can
    # see — under Postgres RLS that is only the tenant's own rows, so a
    # global unique index made every tenant except one collide forever
    # (issue #46).  Migration 0008 applies the same change to migrated DBs.
    __table_args__ = (
        UniqueConstraint("tenant_id", "seq", name="uq_ledger_events_tenant_seq"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=OSS_DEFAULT_TENANT_ID, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    signer_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # seq is an application-assigned monotonically increasing integer and is
    # the sole, authoritative append-order key for the chain: ts is captured
    # once in append_event before its retry loop, so under concurrent
    # writers the loser of a seq race can retry, land a *higher* seq, and
    # still carry an *earlier* ts than the winner — ordering by ts (even as
    # a tie-break) can then pick the wrong row as "latest". seq is only ever
    # claimed via the (tenant_id, seq) unique constraint at insert time, so
    # it always reflects true commit order. get_chain_tip, verify_chain,
    # compute_history_tips, and migration 0009's chain rewrite all order by
    # seq alone. Unique per (tenant_id, seq) — see __table_args__.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DossierIndexDB(Base):
    """
    Lookup index from a dossier_id (and system_id) to its CAS content hash
    and the ledger event that anchors its existence.

    The dossier JSON itself lives in the CAS at `content_hash`; this table only
    holds the metadata needed to find it. Multiple dossiers can exist for the
    same system_id (one per commit_ref); `dossier_id` is globally unique.
    """

    __tablename__ = "dossier_index"

    dossier_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=OSS_DEFAULT_TENANT_ID, index=True
    )
    system_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    commit_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    bundle_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    ledger_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvidenceObjectDB(Base):
    __tablename__ = "evidence_objects"

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_hash: Mapped[str] = mapped_column(
        String(71), nullable=False, unique=True, index=True
    )
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Provenance/freshness metadata (EVID-PROV). Nullable — columns added by
    # migration 0007 (CTRL-STORE).
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    collected_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valid_until: Mapped[str | None] = mapped_column(String(64), nullable=True)
