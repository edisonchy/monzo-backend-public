import asyncio
from datetime import timedelta

from fastapi import HTTPException

from monzo_backend import live
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import DashboardRevision, Transaction, now


def revision():
    with session() as s:
        return s.get(DashboardRevision, 1).revision


def test_revision_commit_noop_rollback_and_bulk(payment):
    before = revision()
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
        ingest(s, "acc_test", {**payment, "id": "tx_second"})
        assert revision() == before  # Other connections cannot see uncommitted changes.
    assert revision() == before + 1
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    assert revision() == before + 1
    with session() as s:
        s.get(Transaction, payment["id"]).amount = -2000
        s.flush()
        s.rollback()
    assert revision() == before + 1
    with session() as s, s.begin():
        tx = s.get(Transaction, payment["id"])
        tx.settled_at = now() - timedelta(seconds=1)
    assert revision() == before + 2


def test_live_endpoints_require_browser_session(client, owner_headers):
    for path in ("revision", "events"):
        response = client.get("/v1/dashboard/" + path, headers=owner_headers)
        assert response.status_code == 401


def test_stream_rechecks_auth_and_releases_slot(monkeypatch):
    class Request:
        async def is_disconnected(self):
            return False

    calls = 0

    def snapshot(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise HTTPException(401)
        return {"revision": 4, "changed_at": None}

    monkeypatch.setattr(live, "snapshot", snapshot)
    monkeypatch.setattr(live, "POLL_SECONDS", 0)
    monkeypatch.setattr(live, "streams", 1)

    async def collect():
        return [item async for item in live.events(Request())]

    result = asyncio.run(collect())
    assert '"revision": 4' in result[0]
    assert result[1].startswith("event: expired")
    assert live.streams == 0


def test_revision_cookie_auth_logout_and_stream_limit(monkeypatch):
    from fastapi.testclient import TestClient

    from monzo_backend.api import app
    from monzo_backend.browser_auth import hash_password
    from monzo_backend.config import settings

    origin = "https://finance.example.test"
    monkeypatch.setenv("MONZO_DASHBOARD_ORIGIN", origin)
    monkeypatch.setenv("MONZO_DASHBOARD_PASSWORD_HASH", hash_password("test-password"))
    settings.cache_clear()
    headers = {"Origin": origin, "X-Dashboard-Request": "1"}
    with TestClient(app, base_url=origin) as client:
        assert (
            client.post(
                "/dashboard/login", headers=headers, json={"password": "test-password"}
            ).status_code
            == 200
        )
        response = client.get("/v1/dashboard/revision")
        assert response.json()["revision"] == revision()
        assert response.headers["cache-control"] == "no-store"
        monkeypatch.setattr(live, "streams", live.MAX_STREAMS)
        assert client.get("/v1/dashboard/events").status_code == 429
        assert client.post("/dashboard/logout", headers=headers).status_code == 200
        assert client.get("/v1/dashboard/revision").status_code == 401
