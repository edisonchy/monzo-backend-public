"""Private durable category operations; additive, writes disabled by default."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f76cb9de4808"
down_revision = "d093ecbf7120"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "category_write_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("id = 1", name="category_control_singleton"),
    )
    op.execute("INSERT INTO category_write_control(id, enabled) VALUES (1, false)")
    op.create_table(
        "category_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("owner_subject", sa.String(512), nullable=False),
        sa.Column("authorized_client", sa.String(512), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("source_batch_id", sa.String(36), sa.ForeignKey("audit_events.id")),
        sa.Column("undone_batch_id", sa.String(36), sa.ForeignKey("audit_events.id")),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("audit_events.id")),
        sa.Column("receipt", postgresql.JSONB()),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("undone_batch_id"),
        sa.CheckConstraint(
            "kind IN ('apply', 'undo') AND origin = 'mcp'", name="category_operation_kind"
        ),
        sa.CheckConstraint(
            "state IN ('prepared', 'committed', 'stale', 'expired')",
            name="category_operation_state",
        ),
        sa.CheckConstraint("expires_at > created_at", name="category_operation_lifetime"),
        sa.CheckConstraint(
            "(state = 'committed') = (receipt IS NOT NULL AND committed_at IS NOT NULL)",
            name="category_operation_receipt",
        ),
    )
    op.create_index("ix_category_operations_batch_id", "category_operations", ["batch_id"])
    # Protect replay identity even from accidental application updates/deletes.
    op.execute("""
        CREATE FUNCTION guard_category_operation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Category operation tombstones must be retained';
            END IF;
            IF OLD.state <> 'prepared' AND (NEW.state <> OLD.state OR
                NEW.receipt IS DISTINCT FROM OLD.receipt OR
                NEW.committed_at IS DISTINCT FROM OLD.committed_at OR
                NEW.batch_id IS DISTINCT FROM OLD.batch_id OR
                NEW.undone_batch_id IS DISTINCT FROM OLD.undone_batch_id OR
                NEW.error_code IS DISTINCT FROM OLD.error_code) THEN
                RAISE EXCEPTION 'Final category operation is immutable';
            END IF;
            IF (to_jsonb(NEW) - ARRAY['state','receipt','committed_at','batch_id','undone_batch_id','command','error_code'])
                IS DISTINCT FROM
                (to_jsonb(OLD) - ARRAY['state','receipt','committed_at','batch_id','undone_batch_id','command','error_code']) THEN
                RAISE EXCEPTION 'Category operation identity is immutable';
            END IF;
            IF NEW.command IS DISTINCT FROM OLD.command AND NOT
                (OLD.state <> 'committed' AND NEW.state IN ('stale','expired') AND
                 NEW.command IS NULL AND OLD.created_at < clock_timestamp() - interval '24 hours') THEN
                RAISE EXCEPTION 'Category operation command is immutable';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER category_operation_guard BEFORE UPDATE OR DELETE ON category_operations
        FOR EACH ROW EXECUTE FUNCTION guard_category_operation();
        REVOKE ALL ON category_operations, category_write_control FROM PUBLIC;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monzo_mcp') THEN
                REVOKE ALL ON category_operations, category_write_control FROM monzo_mcp;
            END IF;
        END $$;
    """)


def downgrade():
    op.drop_table("category_operations")
    op.execute("DROP FUNCTION guard_category_operation()")
    op.drop_table("category_write_control")
