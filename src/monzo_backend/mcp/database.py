"""A separate engine, login and privilege audit; never load worker configuration."""

import time
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

READ_TABLES = (
    "accounts",
    "transactions",
    "merchant_groups",
    "merchant_identities",
    "category_rules",
    "dashboard_revision",
    "reporting_coverage",
    "balance_observations",
    "balance_snapshots",
    "budget_versions",
)


def reporting_engine(url):
    if make_url(url).username != "monzo_mcp":
        raise ValueError("MCP must connect using its dedicated monzo_mcp login")
    engine = create_engine(
        url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "options": "-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000"
        },
    )

    @event.listens_for(engine, "before_cursor_execute")
    def deadline(conn, cursor, statement, parameters, context, executemany):
        remaining = context.execution_options.get("mcp_deadline")
        if remaining is not None:
            ms = min(5000, int((remaining - time.monotonic()) * 1000))
            if ms <= 0:
                raise TimeoutError()
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(ms),))

    try:
        with engine.connect() as conn:
            unsafe = conn.scalar(
                text("""
                SELECT r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls
                       OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member=r.oid)
                       OR has_schema_privilege(current_user, 'public', 'CREATE')
                FROM pg_roles r WHERE r.rolname=current_user
            """)
            )
            if unsafe or conn.scalar(text("SELECT current_user")) != "monzo_mcp":
                raise ValueError("MCP database role has unsafe privileges")
            for table in conn.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            ):
                allowed = table in READ_TABLES
                grants = conn.execute(
                    text("""
                    SELECT has_any_column_privilege(current_user, :t, 'SELECT'),
                           (has_table_privilege(current_user, :t, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR has_any_column_privilege(current_user, :t, 'INSERT,UPDATE,REFERENCES'))
                """),
                    {"t": 'public."' + table.replace('"', '""') + '"'},
                ).one()
                if grants[1] or (grants[0] and not allowed) or (allowed and not grants[0]):
                    raise ValueError("MCP database grants do not match the reporting allowlist")
    except Exception:
        engine.dispose()
        raise
    return engine


@contextmanager
def read_snapshot(engine):
    with Session(engine) as s:
        s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        s.connection().execution_options(mcp_deadline=time.monotonic() + 14)
        try:
            yield s
        finally:
            s.rollback()
