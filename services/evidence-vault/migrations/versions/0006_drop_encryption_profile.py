"""Drop evidence_objects.encryption_profile (EVID-CRYPTO).

The column was declared NOT NULL in migration 0001 and described in the
architecture docs as holding "AES-256-GCM" or "none". No CAS backend has ever
encrypted anything: both the local filesystem store and the Vercel Blob store
write the bytes as given. Nothing in the repository ever set the column and
nothing ever read it — the ``evidence_objects`` table has no INSERT anywhere,
so the column has never held a row.

Its only effect was to tell an auditor reading the schema that an encryption
control existed. Removing it is the honest option: implementing envelope
encryption instead would need a key-management decision this project has not
made, and it would change what the content hash addresses, since ``read()``
verifies integrity by re-hashing the stored bytes.

``downgrade`` restores the column. It re-adds it as nullable first and only
then applies NOT NULL, because the original definition had no server default —
on a table that somehow did contain rows, a direct NOT NULL add would fail.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("evidence_objects") as batch_op:
        batch_op.drop_column("encryption_profile")


def downgrade() -> None:
    with op.batch_alter_table("evidence_objects") as batch_op:
        batch_op.add_column(
            sa.Column("encryption_profile", sa.String(64), nullable=True)
        )
    op.execute(
        "UPDATE evidence_objects SET encryption_profile = 'none' "
        "WHERE encryption_profile IS NULL"
    )
    with op.batch_alter_table("evidence_objects") as batch_op:
        batch_op.alter_column("encryption_profile", nullable=False)
