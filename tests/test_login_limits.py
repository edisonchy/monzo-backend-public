from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_dashboard import HEADERS, ORIGIN, configure_login

from monzo_backend.api import app
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.models import LoginBucket, now


def attempt(peer, forwarded=None, password="wrong"):
    headers = dict(HEADERS)
    if forwarded is not None:
        headers.update({"X-Monzo-Client-IP": forwarded, "X-Forwarded-For": forwarded})
    with TestClient(app, base_url=ORIGIN, client=(peer, 12345)) as client:
        return client.post("/dashboard/login", headers=headers, json={"password": password})


def test_untrusted_headers_cannot_rotate_budget_or_block_another_client(monkeypatch):
    configure_login(monkeypatch)
    for i in range(10):
        assert attempt("192.0.2.1", f"198.51.100.{i}").status_code == 401
    assert attempt("192.0.2.1", "198.51.100.200", "test-password").status_code == 429
    assert attempt("192.0.2.2", password="test-password").status_code == 200
    with session() as s:
        rows = s.scalars(select(LoginBucket)).all()
        assert len(rows) == 2
        assert all(len(row.client_key) == 64 and "." not in row.client_key for row in rows)


def test_trusted_gateway_isolates_clients_and_rejects_missing_or_invalid_identity(monkeypatch):
    configure_login(monkeypatch)
    monkeypatch.setenv("MONZO_DASHBOARD_TRUSTED_PROXY_IPS", '["192.0.2.10"]')
    settings.cache_clear()
    for _ in range(10):
        assert attempt("192.0.2.10", "203.0.113.1").status_code == 401
    assert attempt("192.0.2.10", "203.0.113.1").status_code == 429
    assert attempt("192.0.2.10", "203.0.113.2", "test-password").status_code == 200
    for value in (None, "", "spoof", "203.0.113.1, 203.0.113.2"):
        assert attempt("192.0.2.10", value).status_code == 400


def test_ipv6_prefix_and_mapped_ipv4_cannot_rotate_allowance(monkeypatch):
    configure_login(monkeypatch)
    for i in range(10):
        assert attempt(f"2001:db8:1::{i}").status_code == 401
    assert attempt("2001:db8:1::ffff").status_code == 429
    assert attempt("2001:db8:2::1", password="test-password").status_code == 200
    for _ in range(10):
        assert attempt("192.0.2.1").status_code == 401
    assert attempt("::ffff:192.0.2.1").status_code == 429


def test_atomic_attempts_expiry_and_bounded_cleanup(monkeypatch):
    configure_login(monkeypatch)
    with ThreadPoolExecutor(max_workers=6) as executor:
        statuses = list(executor.map(lambda _: attempt("192.0.2.1").status_code, range(16)))
    assert statuses.count(401) == 10
    assert statuses.count(429) == 6
    with session() as s, s.begin():
        row = s.scalar(select(LoginBucket))
        assert row.attempts == 11
        row.started_at = now() - timedelta(minutes=2)
        s.add_all(
            LoginBucket(client_key=f"old-{i}", started_at=now() - timedelta(hours=2), attempts=10)
            for i in range(150)
        )
    assert attempt("192.0.2.1", password="test-password").status_code == 200
    with session() as s:
        assert s.scalar(select(func.count()).select_from(LoginBucket)) == 51


def test_bad_origin_does_not_consume_allowance(monkeypatch):
    configure_login(monkeypatch)
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.1", 12345)) as client:
        assert client.post("/dashboard/login", json={"password": "wrong"}).status_code == 403
    with session() as s:
        assert s.scalar(select(func.count()).select_from(LoginBucket)) == 0
