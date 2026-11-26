"""Add dashboard categories to the existing spending database."""

import sqlalchemy as sa
from alembic import op

revision = "72a14d90b3fe"
down_revision = "b3836ed417d9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "custom_categories",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("identity", sa.String(160), nullable=False, unique=True),
        sa.Column("colour", sa.String(7), nullable=False),
    )
    op.execute("""CREATE CONSTRAINT TRIGGER dashboard_custom_category AFTER INSERT OR DELETE ON custom_categories
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION advance_dashboard_revision()""")
    # Includes worker, rule, budget and undo writers, even when the category was
    # read before deletion started. Other categories do not acquire this lock.
    op.execute("""CREATE FUNCTION public.check_custom_category_reference() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
          PERFORM pg_advisory_xact_lock_shared(741820, 1);
          IF NOT EXISTS (SELECT FROM public.custom_categories WHERE key=NEW.category) THEN
            RAISE EXCEPTION 'Category no longer exists' USING ERRCODE='23503';
          END IF;
          RETURN NEW;
        END $$""")
    for table in ("transactions", "category_rules", "budget_versions"):
        op.execute(f"""CREATE TRIGGER check_category BEFORE INSERT OR UPDATE OF category ON {table}
            FOR EACH ROW WHEN (left(NEW.category,7)='custom:')
            EXECUTE FUNCTION public.check_custom_category_reference()""")


def downgrade():
    for table in ("transactions", "category_rules", "budget_versions"):
        op.execute(f"DROP TRIGGER check_category ON {table}")
    op.execute("DROP FUNCTION public.check_custom_category_reference()")
    op.drop_table("custom_categories")
