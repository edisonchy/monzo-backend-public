"""Private cross-process category command admission limits."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a87dc0ef5919"
down_revision = "f76cb9de4808"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "category_rate_limits",
        sa.Column("owner_key", sa.String(64), primary_key=True),
        sa.Column("bucket", sa.String(16), primary_key=True),
        sa.Column("attempts", postgresql.JSONB(), nullable=False),
        sa.Column("credits", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
        REVOKE ALL ON category_rate_limits FROM PUBLIC;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monzo_mcp') THEN
                REVOKE ALL ON category_rate_limits FROM monzo_mcp;
            END IF;
        END $$;
    """)


def downgrade():
    op.drop_table("category_rate_limits")
