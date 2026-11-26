"""Publish committed dashboard changes across API and worker processes."""

import sqlalchemy as sa
from alembic import op

revision = "a760b9de4807"
down_revision = "f65ba8cd3706"
branch_labels = None
depends_on = None

# Operational timestamps and rotated credentials are not display changes.
WATCHED = {
    "transactions": "ARRAY['updated_at', 'reporting_checked_at']",
    "accounts": "ARRAY[]::text[]",
    "category_rules": "ARRAY[]::text[]",
    "bank_connections": "ARRAY['access_token', 'refresh_token', 'expires_at']",
    "processing_control": "ARRAY['active_job_id', 'last_success_at']",
    "merchant_research": "ARRAY[]::text[]",
}


def upgrade():
    op.create_table(
        "dashboard_revision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.String(32), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
        CREATE FUNCTION advance_dashboard_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO dashboard_revision (id, revision, transaction_id, changed_at)
            VALUES (1, 1, pg_current_xact_id()::text, clock_timestamp())
            ON CONFLICT (id) DO UPDATE
            SET revision = dashboard_revision.revision + 1,
                transaction_id = EXCLUDED.transaction_id, changed_at = EXCLUDED.changed_at
            WHERE dashboard_revision.transaction_id <> EXCLUDED.transaction_id;
            RETURN NULL;
        END $$
    """)
    # Deferred triggers acquire the revision lock only after business row locks.
    # One increment per committing transaction, including bulk owner changes.
    # Unlike sequence/MAX(event_id), this cannot miss out-of-order commits.
    for table, ignored in WATCHED.items():
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER dashboard_insert_delete
            AFTER INSERT OR DELETE ON {table} DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION advance_dashboard_revision();
            CREATE CONSTRAINT TRIGGER dashboard_update
            AFTER UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW WHEN (
                (to_jsonb(OLD) - {ignored}) IS DISTINCT FROM (to_jsonb(NEW) - {ignored})
            ) EXECUTE FUNCTION advance_dashboard_revision();
        """)
    op.execute("""
        INSERT INTO dashboard_revision VALUES (1, 1, pg_current_xact_id()::text, now())
    """)


def downgrade():
    for table in WATCHED:
        op.execute(f"DROP TRIGGER dashboard_insert_delete ON {table}")
        op.execute(f"DROP TRIGGER dashboard_update ON {table}")
    op.execute("DROP FUNCTION advance_dashboard_revision()")
    op.drop_table("dashboard_revision")
