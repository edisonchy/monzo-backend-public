from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from monzo_backend.api import app
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import claim, finish, notify
from monzo_backend.http import ServiceError
from monzo_backend.models import Job, Notification, now


def test_auth_separates_owner_and_automation(client, automation_headers):
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers=automation_headers).status_code == 200
    for method, path in [
        ("GET", "/v1/transactions"),
        ("GET", "/v1/accounts"),
        ("POST", "/v1/processing/resume"),
        ("POST", "/v1/oauth/start"),
    ]:
        assert client.request(method, path, headers=automation_headers).status_code == 403


def test_webhook_dedupes_and_rejects_unknown_accounts(client):
    url = "/webhooks/monzo/" + settings().webhook_secret.get_secret_value()
    body = {
        "type": "transaction.created",
        "data": {"id": "tx_test", "account_id": "acc_test", "amount": -999},
    }
    assert client.post(url, json=body).status_code == 202
    assert client.post(url, json=body).status_code == 202
    assert client.post("/webhooks/monzo/wrong", json=body).status_code == 404
    body["data"]["account_id"] = "acc_unknown"
    assert client.post(url, json=body).status_code == 404
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.parametrize("prior_attempts", [1, 3])
def test_webhook_redelivery_recovers_failed_fetch_once(client, prior_attempts):
    url = "/webhooks/monzo/" + settings().webhook_secret.get_secret_value()
    body = {"type": "transaction.created", "data": {"id": "tx_retry", "account_id": "acc_test"}}
    assert client.post(url, json=body).status_code == 202
    with session() as s, s.begin():
        job = s.scalars(select(Job)).one()
        job.state, job.attempts = "failed", prior_attempts
    assert client.post(url, json=body).status_code == 202
    with session() as s, s.begin():
        work = claim(s, "sync")
        assert work is not None
    assert client.post(url, json=body).status_code == 202
    with session() as s, s.begin():
        finish(s, work, error=ServiceError("temporary", True))
    assert client.post(url, json=body).status_code == 202
    with session() as s:
        job = s.scalars(select(Job)).one()
        assert job.state == "failed"
        assert job.attempts == prior_attempts + 1


def test_concurrent_webhook_redelivery_after_failure_completes_once(client):
    url = "/webhooks/monzo/" + settings().webhook_secret.get_secret_value()
    body = {"type": "transaction.created", "data": {"id": "tx_race", "account_id": "acc_test"}}
    assert client.post(url, json=body).status_code == 202
    with session() as s, s.begin():
        job = s.scalars(select(Job)).one()
        job.state, job.attempts = "failed", settings().max_attempts
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(client.post, url, json=body) for _ in range(2)]
        assert all(request.result().status_code == 202 for request in requests)
    with session() as s, s.begin():
        work = claim(s, "sync")
        assert work is not None
        finish(s, work)
    assert client.post(url, json=body).status_code == 202
    with session() as s:
        job = s.scalars(select(Job)).one()
        assert job.state == "completed"
        assert job.attempts == settings().max_attempts + 1


@pytest.mark.parametrize("secret", ["A" * 31, "A" * 129, "A" * 32 + "+"])
def test_startup_rejects_unroutable_webhook_secret(monkeypatch, secret):
    monkeypatch.setenv("MONZO_WEBHOOK_SECRET", secret)
    settings.cache_clear()
    with pytest.raises(ValidationError, match="webhook_secret"), TestClient(app):
        pass


@pytest.mark.parametrize("secret", ["Az09_-" + "x" * 26, "x" * 128])
def test_startup_accepts_routable_webhook_secret(monkeypatch, secret):
    monkeypatch.setenv("MONZO_WEBHOOK_SECRET", secret)
    settings.cache_clear()
    with TestClient(app):
        assert settings().webhook_secret.get_secret_value() == secret


def test_notification_lease_and_idempotent_ack(client, automation_headers):
    with session() as s, s.begin():
        notify(s, "incident-1", "Paused", "Sanitised details")
        notify(s, "incident-1", "Paused", "Sanitised details")
    first = client.post("/v1/notifications/claim", headers=automation_headers).json()[
        "notification"
    ]
    assert first
    assert (
        client.post("/v1/notifications/claim", headers=automation_headers).json()["notification"]
        is None
    )
    path = f"/v1/notifications/{first['id']}/ack"
    assert (
        client.post(path, headers=automation_headers, json={"lease_token": "wrong"}).status_code
        == 409
    )
    for _ in range(2):
        assert (
            client.post(
                path, headers=automation_headers, json={"lease_token": first["lease_token"]}
            ).status_code
            == 200
        )
    assert (
        client.post("/v1/notifications/claim", headers=automation_headers).json()["notification"]
        is None
    )


def test_failed_notification_can_be_claimed_after_lease(client, automation_headers):
    with session() as s, s.begin():
        notify(s, "incident", "Paused", "Details")
    first = client.post("/v1/notifications/claim", headers=automation_headers).json()[
        "notification"
    ]
    with session() as s, s.begin():
        note = s.get(Notification, first["id"])
        note.lease_until = note.available_at = now() - timedelta(seconds=1)
    second = client.post("/v1/notifications/claim", headers=automation_headers).json()[
        "notification"
    ]
    assert second["id"] == first["id"]
    assert second["lease_token"] != first["lease_token"]


def test_body_limit(client):
    assert client.post("/v1/sync", content=b"a" * 65537).status_code == 413


def test_schedules_coalesce(client, automation_headers):
    a = client.post("/v1/sync", headers=automation_headers, json={}).json()
    b = client.post("/v1/sync", headers=automation_headers, json={}).json()
    assert a == b


def test_oauth_rejects_unbound_callback(client):
    assert client.get("/oauth/callback?state=untrusted&code=untrusted").status_code == 400


def test_webhook_storage_failure_does_not_acknowledge_or_leave_partial_event(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from monzo_backend.api import app
    from monzo_backend.db import session
    from monzo_backend.models import WebhookEvent

    def fail(*args):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr("monzo_backend.api.enqueue", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/webhooks/monzo/" + settings().webhook_secret.get_secret_value(),
            json={
                "type": "transaction.created",
                "data": {"id": "tx_failure", "account_id": "acc_test"},
            },
        )
    assert response.status_code == 500
    with session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookEvent)) == 0
