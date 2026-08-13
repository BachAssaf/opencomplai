from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
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

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=OSS_DEFAULT_TENANT_ID, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    signer_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # seq is an application-assigned monotonically increasing integer that
    # provides a strict insertion-order tie-breaker when two events share the
    # same `ts` value.  This can happen on platforms where DateTime has
    # coarser-than-microsecond resolution (e.g. SQLite on Windows).
    # append_event() computes MAX(seq)+1 before inserting each event; both
    # get_chain_tip and verify_chain order by (ts, seq) to remain consistent.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
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
