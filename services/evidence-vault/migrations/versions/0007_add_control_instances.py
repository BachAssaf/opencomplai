"""Add control_instances and manifest_fingerprints tables (CTRL-STORE).

evidence-vault is the persistence home for the control instance registry
per D1 — the only service in this stack with a real Postgres+RLS deployment
(TEN-VAULT), so no new service and no second migration/RLS chain. Mirrors
migration 0005's pattern exactly (0005 itself copied 0004): tenant_id +
OSS_DEFAULT_TENANT_ID + index on every new table, Postgres-only RLS policy
scoped to current_setting('app.tenant_id').

Also adds the four EVID-PROV provenance/freshness columns to
evidence_objects (source, source_version, collected_at, valid_until) —
already declared nullable on EvidenceObjectDB in models.py pending this
migration. Added via batch_alter_table like migration 0006, so it also
works against SQLite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

OSS_DEFAULT_TENANT_ID = "oss-default"

_TENANT_SCOPED_TABLES = (
    "control_instances",
    "manifest_fingerprints",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    json_type = sa.JSON().with_variant(JSONB, "postgresql")

    op.create_table(
        "control_instances",
        sa.Column("control_id", sa.String(32), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("system_id", sa.String, nullable=False),
        sa.Column("obligation_id", sa.String, nullable=False),
        sa.Column("article_ref", sa.String, nullable=False),
        sa.Column("owner", sa.String, nullable=True),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("evidence_refs", json_type, nullable=False, server_default="[]"),
        sa.Column("ttl_days", sa.Integer, nullable=True),
        sa.Column("last_assessed_at", sa.String, nullable=True),
        sa.Column("last_evidence_at", sa.String, nullable=True),
        sa.Column("due_at", sa.String, nullable=True),
        sa.Column("waiver_rationale", sa.String, nullable=True),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    op.create_index(
        "ix_control_instances_tenant_id", "control_instances", ["tenant_id"]
    )
    op.create_index(
        "ix_control_instances_system_id", "control_instances", ["system_id"]
    )

    op.create_table(
        "manifest_fingerprints",
        sa.Column(
            "tenant_id",
            sa.String(128),
            primary_key=True,
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("system_id", sa.String, primary_key=True, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    op.create_index(
        "ix_manifest_fingerprints_tenant_id",
        "manifest_fingerprints",
        ["tenant_id"],
    )

    with op.batch_alter_table("evidence_objects") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("source_version", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("collected_at", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("valid_until", sa.String(64), nullable=True))

    if not is_postgres:
        return

    # -- Postgres RLS, reusing the evidence_vault_admin/evidence_vault_app
    # roles created in migration 0004 -------------------------------------
    op.execute(
        "GRANT ALL ON ALL TABLES IN SCHEMA public "
        "TO evidence_vault_admin, evidence_vault_app"
    )

    for table in _TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.execute(
            f"CREATE POLICY {table}_isolation ON {table} "
            f"USING (tenant_id = current_setting('app.tenant_id', true))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        for table in _TENANT_SCOPED_TABLES:
            op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    with op.batch_alter_table("evidence_objects") as batch_op:
        batch_op.drop_column("valid_until")
        batch_op.drop_column("collected_at")
        batch_op.drop_column("source_version")
        batch_op.drop_column("source")

    op.drop_index(
        "ix_manifest_fingerprints_tenant_id", table_name="manifest_fingerprints"
    )
    op.drop_table("manifest_fingerprints")

    op.drop_index("ix_control_instances_system_id", table_name="control_instances")
    op.drop_index("ix_control_instances_tenant_id", table_name="control_instances")
    op.drop_table("control_instances")
