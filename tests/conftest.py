import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("MONZO_OWNER_TOKEN", "test-owner-" + "a" * 40)
os.environ.setdefault("MONZO_AUTOMATION_TOKEN", "test-automation-" + "b" * 40)
os.environ.setdefault("MONZO_WEBHOOK_SECRET", "test-webhook-" + "c" * 40)
os.environ.setdefault("MONZO_ENCRYPTION_KEY", Fernet.generate_key().decode())

from monzo_backend.api import app  # noqa: E402
from monzo_backend.config import settings  # noqa: E402
from monzo_backend.db import (  # noqa: E402
    engine,
    require_test_database,
    require_test_database_url,
    session,
)  # noqa: E402
from monzo_backend.models import Account, Connection, Control, now  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    settings.cache_clear()
    require_test_database_url(engine().url)
    with engine().begin() as conn:
        require_test_database(conn)
        tables = (
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename != 'alembic_version'"
                )
            )
            .scalars()
            .all()
        )
        if tables:
            conn.execute(text("TRUNCATE " + ",".join('"' + t + '"' for t in tables) + " CASCADE"))
    with session() as s, s.begin():
        s.add(Control(id=1))
        s.add(
            Connection(
                id=1,
                user_id="user_test",
                access_token="encrypted",
                expires_at=now(),
                state="healthy",
            )
        )
        s.flush()
        s.add(Account(id="acc_test", connection_id=1, kind="uk_retail"))
    yield
    settings.cache_clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def owner_headers():
    return {"Authorization": "Bearer " + settings().owner_token.get_secret_value()}


@pytest.fixture
def automation_headers():
    return {"Authorization": "Bearer " + settings().automation_token.get_secret_value()}


@pytest.fixture
def payment():
    return {
        "id": "tx_test",
        "account_id": "acc_test",
        "amount": -1234,
        "currency": "GBP",
        "created": "2026-10-15T11:00:00Z",
        "settled": "",
        "category": "general",
        "description": "Private text must not go to the model",
        "merchant": {"id": "merch_test", "name": "Test Shop"},
    }
