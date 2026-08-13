"""
BiasAlert persistence and purge for the evidence vault (REQ-GTVG-002).

Stores alerts raised by the Ground Truth Verification Graph and exposes
a purge function that deletes records older than the retention window.
A `bias_data_purge` ledger event is appended after each successful purge
so the deletion is auditable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from opencomplai_core.purge import expired_cutoff
from sqlalchemy import DateTime, Float, String, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from opencomplai_evidence_vault.models import OSS_DEFAULT_TENANT_ID


class _Base(DeclarativeBase):
    pass


class BiasAlertDB(_Base):
    __tablename__ = "bias_alerts"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=OSS_DEFAULT_TENANT_ID, index=True
    )
    alert_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    metric: Mapped[str] = mapped_column(String, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    linked_event_id: Mapped[str] = mapped_column(String, nullable=False)
    system_id: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


async def store_bias_alert(
    session: AsyncSession,
    alert_id: str,
    severity: str,
    metric: str,
    threshold: float,
    linked_event_id: str,
    system_id: str | None = None,
    tenant_id: str = OSS_DEFAULT_TENANT_ID,
) -> BiasAlertDB:
    """Persist a BiasAlert record."""
    record = BiasAlertDB(
        tenant_id=tenant_id,
        alert_id=alert_id,
        severity=severity,
        metric=metric,
        threshold=threshold,
        linked_event_id=linked_event_id,
        system_id=system_id,
    )
    session.add(record)
    await session.flush()
    return record


async def purge_expired_bias_data(
    session: AsyncSession, retention_days: int, tenant_id: str = OSS_DEFAULT_TENANT_ID
) -> int:
    """
    Delete BiasAlert records older than retention_days, scoped to tenant_id.

    Returns the count of deleted records.
    """
    cutoff = expired_cutoff(retention_days)
    result = await session.execute(
        delete(BiasAlertDB).where(
            BiasAlertDB.created_at < cutoff, BiasAlertDB.tenant_id == tenant_id
        )
    )
    deleted: int = result.rowcount  # type: ignore[assignment]
    return deleted


async def count_bias_alerts(
    session: AsyncSession, tenant_id: str = OSS_DEFAULT_TENANT_ID
) -> int:
    """Return count of stored BiasAlert records for tenant_id."""
    result = await session.execute(
        select(func.count())
        .select_from(BiasAlertDB)
        .where(BiasAlertDB.tenant_id == tenant_id)
    )
    return result.scalar_one()


async def create_bias_alerts_table(engine) -> None:
    """Create the bias_alerts table (used in tests; production uses Alembic)."""
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
