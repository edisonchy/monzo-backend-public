"""Persist a fair refresh schedule for unresolved older bank entries."""

import sqlalchemy as sa
from alembic import op

revision = "b871caef5908"
down_revision = "a760b9de4807"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("transactions", sa.Column("pending_check_after", sa.DateTime(timezone=True)))
    op.execute("DROP TRIGGER dashboard_update ON transactions")
    op.execute("""
        CREATE CONSTRAINT TRIGGER dashboard_update AFTER UPDATE ON transactions
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (
          (to_jsonb(OLD) - ARRAY['updated_at','reporting_checked_at','pending_check_after'])
          IS DISTINCT FROM
          (to_jsonb(NEW) - ARRAY['updated_at','reporting_checked_at','pending_check_after'])
        ) EXECUTE FUNCTION advance_dashboard_revision()
    """)


def downgrade():
    op.execute("DROP TRIGGER dashboard_update ON transactions")
    op.drop_column("transactions", "pending_check_after")
    op.execute("""
        CREATE CONSTRAINT TRIGGER dashboard_update AFTER UPDATE ON transactions
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (
          (to_jsonb(OLD) - ARRAY['updated_at','reporting_checked_at']) IS DISTINCT FROM
          (to_jsonb(NEW) - ARRAY['updated_at','reporting_checked_at'])
        ) EXECUTE FUNCTION advance_dashboard_revision()
    """)
