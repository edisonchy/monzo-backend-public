"""Record successful delivery of each automated reconciliation request."""

import sqlalchemy as sa
from alembic import op

revision = "ab830ff0942e"
down_revision = "fa54d730c686"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "monzo_scheduler_heartbeats",
        sa.Column("name", sa.String(16), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Historical jobs also came from on-demand calls; only new timer requests prove liveness.


def downgrade():
    op.drop_table("monzo_scheduler_heartbeats")
