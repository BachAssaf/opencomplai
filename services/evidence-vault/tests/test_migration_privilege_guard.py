"""Static guard: no migration after 0009 may use the raw ``GRANT ALL ON ALL
TABLES IN SCHEMA public`` idiom directly.

Postgres applies that statement to every table already in the schema, not
just newly added ones — so a future migration copy-pasting the 0004/0005/
0007 idiom would silently re-grant UPDATE/DELETE/TRUNCATE on ledger_events
to evidence_vault_app, undoing migration 0009's append-only lockdown
(TRUNCATE bypasses RLS entirely). Such migrations must go through
migrations/_privileges.py's grant_all_tables_preserving_ledger_lockdown,
which re-revokes immediately after granting.

Runs without a database, so it executes in every CI lane.
"""

from __future__ import annotations

import re
from pathlib import Path

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
_RAW_GRANT_ALL = re.compile(r"GRANT ALL ON ALL TABLES IN SCHEMA public")


def test_no_migration_after_0009_uses_raw_grant_all_tables():
    offenders = []
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        revision_num = path.stem.split("_", 1)[0]
        if not revision_num.isdigit() or int(revision_num) <= 9:
            continue
        if _RAW_GRANT_ALL.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} use the raw GRANT ALL idiom directly — use "
        "migrations/_privileges.py's "
        "grant_all_tables_preserving_ledger_lockdown instead, or migration "
        "0009's ledger_events lockdown is silently undone"
    )
