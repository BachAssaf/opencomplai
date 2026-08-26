"""Shared helpers for migrations that touch Postgres role privileges.

Migration 0009 revokes UPDATE/DELETE/TRUNCATE on ledger_events from the
request-facing evidence_vault_app role: the application only ever INSERTs
and SELECTs ledger rows, and TRUNCATE bypasses RLS entirely. That revoke is
not durable on its own — Postgres's ``GRANT ALL ON ALL TABLES IN SCHEMA
public`` targets every table already in the schema, so the idiom migrations
0004/0005/0007 used when adding a table would, if copy-pasted into any
migration numbered after 0009, silently re-grant all three privileges and
reopen the exact tamper vector 0009 closed.

Any migration after 0009 that needs a blanket grant MUST call
:func:`grant_all_tables_preserving_ledger_lockdown` instead of executing the
raw statement (tests/test_migration_privilege_guard.py enforces this
statically).
"""

from __future__ import annotations

from alembic import op


def grant_all_tables_preserving_ledger_lockdown(roles: str) -> None:
    """GRANT ALL ON ALL TABLES IN SCHEMA public TO *roles*, then immediately
    re-REVOKE the ledger_events privileges migration 0009 locked down for
    evidence_vault_app, if that role is among *roles*."""
    op.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {roles}")
    if "evidence_vault_app" in roles:
        op.execute(
            "REVOKE UPDATE, DELETE, TRUNCATE ON ledger_events FROM evidence_vault_app"
        )
