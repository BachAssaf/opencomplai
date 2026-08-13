"""Add durable HITL/override/eval-cache persistence tables (PERSIST-RISK).

risk-engine previously held its review queue, override idempotency cache,
and eval-run cache as process-local dicts — a restart or a second replica
silently dropped in-flight review items and broke idempotency guarantees.
These four tables give that state a durable home in evidence-vault, the
only service in this stack with a real Postgres+RLS deployment (TEN-VAULT),
rather than standing up a second migration/RLS chain in risk-engine.

Mirrors migration 0004's RLS pattern exactly: tenant_id + index on every
table, Postgres-only RLS policy scoped to current_setting('app.tenant_id').
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

OSS_DEFAULT_TENANT_ID = "oss-default"

_TENANT_SCOPED_TABLES = (
    "review_items",
    "review_contexts",
    "accepted_overrides",
    "completed_evals",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    json_type = sa.JSON().with_variant(JSONB, "postgresql")

    op.create_table(
        "review_items",
        sa.Column("review_id", sa.String, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("system_id", sa.String, nullable=False),
        sa.Column("commit_ref", sa.String, nullable=False),
        sa.Column("reason", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("payload_ref", sa.String, nullable=False),
        sa.Column("context_ref", sa.String, nullable=False),
        sa.Column("reviewer_group", sa.String, nullable=True),
        sa.Column("assigned_to", sa.String, nullable=True),
        sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("expires_at", sa.String, nullable=True),
        sa.Column("decided_at", sa.String, nullable=True),
        sa.Column("linked_override_id", sa.String, nullable=True),
    )
    op.create_index("ix_review_items_tenant_id", "review_items", ["tenant_id"])
    op.create_index("ix_review_items_state", "review_items", ["state"])

    op.create_table(
        "review_contexts",
        sa.Column("context_ref", sa.String, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("context_json", json_type, nullable=False),
    )
    op.create_index("ix_review_contexts_tenant_id", "review_contexts", ["tenant_id"])

    op.create_table(
        "accepted_overrides",
        sa.Column("idempotency_key", sa.String, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("payload_fingerprint", sa.String, nullable=False),
        sa.Column("response_json", json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_accepted_overrides_tenant_id", "accepted_overrides", ["tenant_id"]
    )

    op.create_table(
        "completed_evals",
        sa.Column("eval_run_id", sa.String, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=False,
            server_default=OSS_DEFAULT_TENANT_ID,
        ),
        sa.Column("result_json", json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_completed_evals_tenant_id", "completed_evals", ["tenant_id"])

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

    op.drop_index("ix_completed_evals_tenant_id", table_name="completed_evals")
    op.drop_table("completed_evals")

    op.drop_index("ix_accepted_overrides_tenant_id", table_name="accepted_overrides")
    op.drop_table("accepted_overrides")

    op.drop_index("ix_review_contexts_tenant_id", table_name="review_contexts")
    op.drop_table("review_contexts")

    op.drop_index("ix_review_items_state", table_name="review_items")
    op.drop_index("ix_review_items_tenant_id", table_name="review_items")
    op.drop_table("review_items")
