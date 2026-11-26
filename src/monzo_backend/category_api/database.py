"""Own engine and bounded transactions; no bank, browser or automation secrets."""

import time
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from monzo_backend.category_operations import OperationError


def command_engine(url):
    engine = create_engine(
        url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=5000 -c lock_timeout=1000",
        },
    )

    def bound(conn):
        deadline = conn.info.get("category_deadline")
        if deadline is None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OperationError("deadline_exceeded")
        milliseconds = max(1, min(5000, int(remaining * 1000)))
        with conn.connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout = {milliseconds}")

    @event.listens_for(engine, "before_cursor_execute")
    def before_execute(conn, cursor, statement, parameters, context, executemany):
        bound(conn)

    @event.listens_for(engine, "commit")
    def before_commit(conn):
        # Deferred revision triggers run during commit and need the same budget.
        bound(conn)

    return engine


def sessions(engine, deadline):
    @contextmanager
    def factory():
        with engine.connect() as conn:
            conn.info["category_deadline"] = deadline
            try:
                with Session(bind=conn, expire_on_commit=False) as s:
                    yield s
            finally:
                conn.info.pop("category_deadline", None)

    return factory
