"""Add tenant_id + Postgres RLS to evidence-vault's four persisted tables.

TEN-VAULT: evidence-vault was the only backend service with real Postgres
persistence but no tenant_id anywhere — one deployment was a single shared
ledger/badge/dossier namespace. Adds tenant_id to ledger_events and
dossier_index (both already Alembic-managed), and creates bias_alerts and
compliance_badges here for the first time under Alembic — previously they
only existed via SQLAlchemy `create_all()` in main.py's lifespan(), never
migrated (see BLOCKERS.md's dr-test.yml note: they were never backed up
either, as a direct consequence).

RLS mirrors dashboard_db's schema.sql pattern exactly: a `dashboard_app`-style
non-BYPASSRLS role scoped by `current_setting('app.tenant_id')`, and an
`evidence_vault_admin` BYPASSRLS role for cross-tenant/ops paths. Both
policies + roles are Postgres-only; SQLite (used in unit tests) has no RLS
concept and the tenant_id column there is enforced purely at the query layer
(see get_session/tenant scoping in main.py).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

OSS_DEFAULT_TENANT_ID = "oss-default"

_TENANT_SCOPED_TABLES = (
    "ledger_events",
    "dossier_index",
    "bias_alerts",
    "compliance_badges",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # -- ledger_events: add tenant_id -----------------------------------
    op.add_column(
        "ledger_events",
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
    )
    op.create_index("ix_ledger_events_tenant_id", "ledger_events", ["tenant_id"])

    # -- dossier_index: add tenant_id ------------------------------------
    op.add_column(
        "dossier_index",
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
    )
    op.create_index("ix_dossier_index_tenant_id", "dossier_index", ["tenant_id"])

    # -- bias_alerts: first-ever Alembic migration for this table --------
    op.create_table(
        "bias_alerts",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("alert_id", sa.String, nullable=False),
        sa.Column("severity", sa.String, nullable=False),
        sa.Column("metric", sa.String, nullable=False),
        sa.Column("threshold", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("linked_event_id", sa.String, nullable=False),
        sa.Column("system_id", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_bias_alerts_alert_id", "bias_alerts", ["alert_id"])
    op.create_index("ix_bias_alerts_tenant_id", "bias_alerts", ["tenant_id"])

    # -- compliance_badges: first-ever Alembic migration for this table --
    op.create_table(
        "compliance_badges",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("badge_id", sa.String, nullable=False),
        sa.Column("system_id", sa.String, nullable=False),
        sa.Column("bundle_checksum", sa.String, nullable=False),
        sa.Column("issued_at", sa.String, nullable=False),
        sa.Column("status_artifact_hash", sa.String, nullable=False),
        sa.Column("signature", sa.String, nullable=True),
    )
    op.create_index("ix_compliance_badges_badge_id", "compliance_badges", ["badge_id"])
    op.create_index(
        "ix_compliance_badges_tenant_id", "compliance_badges", ["tenant_id"]
    )
    op.create_index(
        "ix_compliance_badges_tenant_badge",
        "compliance_badges",
        ["tenant_id", "badge_id"],
        unique=True,
    )

    if not is_postgres:
        return

    # -- Postgres roles + RLS, mirroring dashboard_db/schema.sql ---------
    op.execute(
        """
        DO $$ BEGIN
            CREATE ROLE evidence_vault_admin BYPASSRLS NOLOGIN;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE ROLE evidence_vault_app NOSUPERUSER NOLOGIN;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )
    op.execute(
        "GRANT USAGE ON SCHEMA public TO evidence_vault_admin, evidence_vault_app"
    )
    op.execute(
        "GRANT ALL ON ALL TABLES IN SCHEMA public "
        "TO evidence_vault_admin, evidence_vault_app"
    )
    op.execute(
        "GRANT ALL ON ALL SEQUENCES IN SCHEMA public "
        "TO evidence_vault_admin, evidence_vault_app"
    )
    op.execute(
        """
        DO $$ BEGIN
            EXECUTE format('GRANT evidence_vault_admin TO %I', current_user);
            EXECUTE format('GRANT evidence_vault_app   TO %I', current_user);
        END $$;
        """
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

    op.drop_index("ix_compliance_badges_tenant_badge", table_name="compliance_badges")
    op.drop_index("ix_compliance_badges_tenant_id", table_name="compliance_badges")
    op.drop_index("ix_compliance_badges_badge_id", table_name="compliance_badges")
    op.drop_table("compliance_badges")

    op.drop_index("ix_bias_alerts_tenant_id", table_name="bias_alerts")
    op.drop_index("ix_bias_alerts_alert_id", table_name="bias_alerts")
    op.drop_table("bias_alerts")

    op.drop_index("ix_dossier_index_tenant_id", table_name="dossier_index")
    op.drop_column("dossier_index", "tenant_id")

    op.drop_index("ix_ledger_events_tenant_id", table_name="ledger_events")
    op.drop_column("ledger_events", "tenant_id")
