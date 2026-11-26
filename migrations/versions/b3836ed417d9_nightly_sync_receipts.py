"""Keep the committed response for retries of the same nightly n8n execution."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b3836ed417d9"
down_revision = "ab830ff0942e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "monzo_nightly_sync_receipts",
        sa.Column("run_id", sa.String(32), primary_key=True),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("job_ids", JSONB, nullable=False),
    )


def downgrade():
    op.drop_table("monzo_nightly_sync_receipts")
