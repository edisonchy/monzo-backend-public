from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from monzo_backend.config import settings


@lru_cache
def engine():
    return create_engine(
        settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
        hide_parameters=True,
        connect_args={"options": "-c statement_timeout=15000 -c lock_timeout=5000"},
    )


def session() -> Session:
    return Session(engine(), expire_on_commit=False)


def is_transaction_contention(exc):
    return getattr(exc.orig, "sqlstate", None) in ("40001", "40P01", "55P03")


def require_test_database_url(url):
    if make_url(url).database != "monzo_test":
        raise ValueError("Tests require a dedicated database named monzo_test")


def require_test_database(connection):
    require_test_database_url(connection.engine.url)
    if connection.scalar(text("SELECT current_database()")) != "monzo_test":
        raise ValueError("Connected database must be monzo_test")
