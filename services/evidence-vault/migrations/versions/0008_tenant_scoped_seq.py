"""Scope the ledger_events seq uniqueness to (tenant_id, seq).

Issue #46: _next_seq() computes MAX(seq)+1 from the rows the calling session
can see.  Under Postgres RLS that is only the caller's own tenant's rows,
but 0003's ix_ledger_events_seq enforced uniqueness across the whole table —
so every tenant except the one holding the globally-highest seq computed a
colliding value, and its appends failed with IntegrityError on every retry.

Existing rows carry globally-unique seq values, which are trivially unique
within each tenant, so no data rewrite is needed and per-tenant (ts, seq)
ordering is preserved.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_ledger_events_seq", table_name="ledger_events")
    op.create_index(
        "uq_ledger_events_tenant_seq",
        "ledger_events",
        ["tenant_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    # Note: recreating the global unique index fails if different tenants
    # have allocated the same seq value since upgrading — expected, since the
    # global constraint is exactly what made multi-tenant appends impossible.
    op.drop_index("uq_ledger_events_tenant_seq", table_name="ledger_events")
    op.create_index("ix_ledger_events_seq", "ledger_events", ["seq"], unique=True)
