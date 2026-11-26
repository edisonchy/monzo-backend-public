from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select

from monzo_backend import monzo
from monzo_backend.bank_oauth import sha
from monzo_backend.config import settings
from monzo_backend.coverage import record_completed_window
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import (
    Account,
    Connection,
    Control,
    Job,
    Notification,
    OAuthState,
    ReportingCoverage,
    Rule,
    Transaction,
    now,
)


def test_unavailable_health_returns_safe_retryable_response(client, owner_headers, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def unavailable(_):
        raise OperationalError("secret SQL", {}, Exception("secret connection"))

    monkeypatch.setattr("monzo_backend.health.snapshot", unavailable)
    result = client.get("/v1/status", headers=owner_headers)
    assert result.status_code == 503
    assert "secret" not in result.text
    assert result.headers["cache-control"] == "no-store"


def test_browser_reconnect_requires_csrf_and_same_origin_callback(monkeypatch, respx_mock):
    from fastapi.testclient import TestClient

    from monzo_backend.api import app
    from monzo_backend.browser_auth import hash_password

    origin = "https://finance.example.test"
    monkeypatch.setenv("MONZO_DASHBOARD_ORIGIN", origin)
    monkeypatch.setenv("MONZO_DASHBOARD_PASSWORD_HASH", hash_password("test-password"))
    monkeypatch.setenv("MONZO_CLIENT_ID", "synthetic")
    monkeypatch.setenv("MONZO_CLIENT_SECRET", "synthetic-secret")
    settings.cache_clear()
    headers = {"Origin": origin, "X-Dashboard-Request": "1"}
    with TestClient(app, base_url=origin, follow_redirects=False) as browser:
        assert browser.post("/v1/oauth/start", headers=headers).status_code == 401
        assert (
            browser.post(
                "/dashboard/login", headers=headers, json={"password": "test-password"}
            ).status_code
            == 200
        )
        assert browser.post("/v1/oauth/start").status_code == 403
        assert browser.post("/v1/oauth/start", headers=headers).status_code == 409
        monkeypatch.setenv("MONZO_REDIRECT_URI", origin + "/oauth/callback")
        settings.cache_clear()
        result = browser.post("/v1/oauth/start", headers=headers)
        assert result.status_code == 200
        assert "Secure" in result.headers["set-cookie"]
        state = parse_qs(urlsplit(result.json()["authorization_url"]).query)["state"][0]
        mock_tokens(respx_mock)
        callback = browser.get(f"/oauth/callback?state={state}&code=synthetic")
        assert callback.status_code == 303
        assert callback.headers["location"] == "/settings"
        assert "code=" not in callback.headers["location"]


def start(client, headers, monkeypatch):
    monkeypatch.setenv("MONZO_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("MONZO_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv("MONZO_SECURE_COOKIES", "false")
    settings.cache_clear()
    result = client.post("/v1/oauth/start", headers=headers)
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"]
    return parse_qs(urlsplit(result.json()["authorization_url"]).query)["state"][0]


def test_health_distinguishes_stale_quiet_and_failed_jobs(client, owner_headers):
    stamp = now()
    with session() as s, s.begin():
        stale = stamp - timedelta(minutes=46)
        s.get(Account, "acc_test").last_sync_at = stale
        s.add(Account(id="acc_flex", connection_id=1, kind="uk_monzo_flex", last_sync_at=stamp))
        s.flush()
        for aid, end in (("acc_test", stale), ("acc_flex", stamp)):
            record_completed_window(
                s, aid, {"since": (end - timedelta(days=1)).isoformat(), "before": end.isoformat()}
            )
        s.add(
            Job(
                queue="category",
                kind="classify",
                dedupe_key="failed",
                state="failed",
                error_code="secret-internal-detail",
            )
        )
        s.add(
            Job(
                queue="sync",
                kind="sync_page",
                dedupe_key="delayed",
                created_at=stamp - timedelta(hours=1),
            )
        )
        s.add(
            Notification(
                event_key="alert",
                subject="private",
                body="private",
                created_at=stamp - timedelta(minutes=6),
                attempts=2,
            )
        )
        s.get(Control, 1).state = "recovering"
    result = client.get("/v1/status", headers=owner_headers)
    data = result.json()
    accounts = {a["id"]: a for a in data["accounts"]}
    assert accounts["acc_test"]["state"] == "stale"
    assert accounts["acc_flex"]["state"] == "current"
    assert data["connection"]["checked_through"] == accounts["acc_test"]["checked_through"]
    assert accounts["acc_flex"]["last_webhook_at"] is None
    assert data["categorisation"]["pending"] == 0
    assert data["categorisation"]["state"] == "recovering"
    assert data["queues"]["category"]["failed"] == 1
    assert data["queues"]["sync"]["delayed"]
    assert data["notifications"]["state"] == "delayed"
    assert "private" not in result.text and "secret-internal-detail" not in result.text
    with session() as s, s.begin():
        record_completed_window(
            s, "acc_test", {"since": stale.isoformat(), "before": stamp.isoformat()}
        )
        s.get(Account, "acc_test").last_sync_at = now()
        s.scalar(select(Notification)).delivered_at = now()
        s.scalar(select(Job).where(Job.queue == "sync")).state = "completed"
    data = client.get("/v1/status", headers=owner_headers).json()
    assert all(a["state"] == "current" for a in data["accounts"])
    assert data["notifications"]["state"] == "idle"
    assert data["notifications"]["last_acknowledged_at"]
    assert not data["queues"]["sync"]["delayed"]


def test_webhook_receipt_visible_before_payment_fetch(client, owner_headers):
    result = client.post(
        "/webhooks/monzo/" + settings().webhook_secret.get_secret_value(),
        json={
            "type": "transaction.created",
            "data": {"id": "tx_notfetched", "account_id": "acc_test"},
        },
    )
    assert result.status_code == 202
    data = client.get("/v1/status", headers=owner_headers).json()
    assert data["accounts"][0]["last_webhook_at"]
    with session() as s:
        assert s.get(Transaction, "tx_notfetched") is None


@pytest.mark.parametrize("state", ["paused", "recovering", "running"])
def test_health_reports_classifier_state_without_assuming_liveness(client, owner_headers, state):
    with session() as s, s.begin():
        s.get(Control, 1).state = state
        s.add(
            Job(
                queue="category",
                kind="classify",
                dedupe_key="expired",
                state="running",
                lease_until=now() - timedelta(seconds=1),
            )
        )
    data = client.get("/v1/status", headers=owner_headers).json()
    assert data["categorisation"]["state"] == state
    assert data["queues"]["category"]["overdue"] == 1


@pytest.mark.parametrize(
    "case", ["denied", "missing_code", "expired", "wrong_binding", "superseded"]
)
def test_oauth_rejections_preserve_connection(client, owner_headers, monkeypatch, respx_mock, case):
    state = start(client, owner_headers, monkeypatch)
    if case == "expired":
        with session() as s, s.begin():
            s.get(OAuthState, sha(state)).expires_at = now() - timedelta(seconds=1)
    if case == "wrong_binding":
        client.cookies.clear()
    if case == "superseded":
        start(client, owner_headers, monkeypatch)
    query = (
        "&error=access_denied"
        if case == "denied"
        else ""
        if case == "missing_code"
        else "&code=synthetic"
    )
    result = client.get("/oauth/callback?state=" + state + query)
    assert result.status_code == (200 if case in ("denied", "missing_code") else 400)
    assert not respx_mock.calls
    with session() as s:
        assert s.get(Connection, 1).access_token == "encrypted"
        assert s.scalar(select(func.count()).select_from(Job)) == 0


def mock_tokens(respx_mock, user="user_test"):
    route = respx_mock.post("https://api.monzo.com/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "fresh", "refresh_token": "refresh", "expires_in": 3600}
        )
    )
    respx_mock.get("https://api.monzo.com/ping/whoami").mock(
        return_value=httpx.Response(200, json={"authenticated": True, "user_id": user})
    )
    return route


def test_oauth_success_is_single_use_encrypted_and_queues_bounded_recovery(
    client, owner_headers, monkeypatch, respx_mock
):
    state = start(client, owner_headers, monkeypatch)
    route = mock_tokens(respx_mock)
    result = client.get(f"/oauth/callback?state={state}&code=synthetic")
    assert result.status_code == 200
    assert "fresh" not in result.text
    assert client.get(f"/oauth/callback?state={state}&code=synthetic").status_code == 400
    assert route.call_count == 1
    with session() as s:
        assert monzo.decrypt(s.get(Connection, 1).access_token) == "fresh"
        assert s.get(OAuthState, sha(state)).outcome == "connected"
        assert s.scalar(select(Job)).payload == {"reconnect": True}


@pytest.mark.parametrize(
    "case", ["wrong_owner", "timeout", "invalid_tokens", "superseded_during_exchange"]
)
def test_failed_exchange_does_not_replace_tokens(
    client, owner_headers, monkeypatch, respx_mock, case
):
    state = start(client, owner_headers, monkeypatch)
    if case == "wrong_owner":
        mock_tokens(respx_mock, "other-user")
    elif case == "superseded_during_exchange":

        def exchange(request):
            start(client, owner_headers, monkeypatch)
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

        respx_mock.post("https://api.monzo.com/oauth2/token").mock(side_effect=exchange)
        respx_mock.get("https://api.monzo.com/ping/whoami").mock(
            return_value=httpx.Response(200, json={"authenticated": True, "user_id": "user_test"})
        )
    else:
        respx_mock.post("https://api.monzo.com/oauth2/token").mock(
            return_value=httpx.Response(
                504 if case == "timeout" else 200, json={"expires_in": "invalid"}
            )
        )
    assert client.get(f"/oauth/callback?state={state}&code=synthetic").status_code == 200
    with session() as s:
        assert s.get(Connection, 1).access_token == "encrypted"
        assert s.get(OAuthState, sha(state)).outcome in ("failed", "cancelled")


def test_interrupted_exchange_expires_and_cannot_replay(client, owner_headers, monkeypatch):
    state = start(client, owner_headers, monkeypatch)
    with session() as s, s.begin():
        row = s.get(OAuthState, sha(state))
        row.used, row.outcome, row.expires_at = True, "exchanging", now() - timedelta(seconds=1)
    assert (
        client.get("/v1/status", headers=owner_headers).json()["connection"]["reconnect_state"]
        == "expired"
    )
    assert client.get(f"/oauth/callback?state={state}&code=synthetic").status_code == 400


def test_reconnection_preserves_history_rules_and_avoids_ai_backfill(monkeypatch, payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", payment)
        tx.category, tx.category_source, tx.category_status = "groceries", "manual", "assigned"
        s.get(Account, "acc_test").history_status = "imported"
        s.add(Rule(merchant_id="merch_test", category="groceries", effective_from=now()))
    monkeypatch.setattr(
        monzo,
        "bank_get",
        lambda *args: {
            "accounts": [
                {"id": "acc_test", "type": "uk_retail", "created": "2015-01-01T00:00:00Z"},
                {"id": "acc_rewards", "type": "uk_rewards"},
            ]
        },
    )
    monzo.execute({"id": "reconnect", "kind": "discover", "payload": {"reconnect": True}})
    with session() as s:
        job = s.scalar(select(Job).where(Job.queue == "sync"))
        payload = dict(job.payload)
        work = {"id": job.id, "kind": job.kind, "payload": payload}
        assert not payload["historical"] and payload["reconnect_cutoff"]
        assert (now() - monzo.datetime.fromisoformat(payload["since"])).days == 7
        assert s.get(Account, "acc_test").history_status == "imported"
        assert s.get(Account, "acc_rewards") is None
        before = s.scalar(select(func.count()).select_from(Job).where(Job.queue == "category"))
    unnamed = {**payment, "id": "tx_new", "merchant": None}
    monkeypatch.setattr(monzo, "bank_get", lambda *args: {"transactions": [payment, unnamed]})
    monzo.execute(work)
    monzo.execute(work)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 2
        tx = s.get(Transaction, payment["id"])
        assert (tx.category, tx.category_source, tx.category_status) == (
            "groceries",
            "manual",
            "assigned",
        )
        assert s.get(Transaction, "tx_new").category_status == "needs_review"
        assert (
            s.scalar(select(func.count()).select_from(Job).where(Job.queue == "category")) == before
        )
        assert s.scalar(select(Rule)).category == "groceries"
        assert s.get(Account, "acc_test").last_sync_at
        assert s.scalar(select(func.count()).select_from(ReportingCoverage)) == 1


def test_obsolete_refresh_failure_cannot_disable_new_credentials(monkeypatch, respx_mock):
    monkeypatch.setenv("MONZO_CLIENT_ID", "client")
    monkeypatch.setenv("MONZO_CLIENT_SECRET", "secret")
    settings.cache_clear()
    with session() as s, s.begin():
        c = s.get(Connection, 1)
        c.access_token, c.refresh_token = monzo.encrypt("old"), monzo.encrypt("old-refresh")

    def rejected(request):
        with session() as s, s.begin():
            c = s.get(Connection, 1)
            c.access_token, c.state = monzo.encrypt("new"), "awaiting_approval"
        return httpx.Response(400)

    respx_mock.post("https://api.monzo.com/oauth2/token").mock(side_effect=rejected)
    with pytest.raises(monzo.ServiceError):
        monzo.access_token()
    with session() as s:
        assert s.get(Connection, 1).state == "awaiting_approval"
        assert s.scalar(select(func.count()).select_from(Notification)) == 0


def test_quality_separates_suggestions_and_deterministic_unnamed_evidence(
    client, owner_headers, automation_headers, payment
):
    with session() as s, s.begin():
        for i, source in enumerate(("manual", "rule", "monzo", "ai")):
            tx = ingest(s, "acc_test", {**payment, "id": f"tx_{i}"})
            tx.category, tx.category_status, tx.category_source = "groceries", "assigned", source
        for i, bank in enumerate(("groceries", "general")):
            tx = ingest(
                s,
                "acc_test",
                {**payment, "id": f"tx_unnamed{i}", "merchant": None, "category": bank},
            )
            tx.category_source, tx.category_status = "review", "needs_review"
    assert client.get("/v1/dashboard/quality").status_code == 401
    assert client.get("/v1/dashboard/quality", headers=automation_headers).status_code == 403
    q = client.get("/v1/dashboard/quality", headers=owner_headers).json()
    assert (q["total"], q["verified"], q["ai_suggested"], q["needs_review"], q["pending"]) == (
        6,
        3,
        1,
        2,
        0,
    )
    assert (q["unnamed"], q["unnamed_with_bank_or_rule_evidence"], q["unnamed_manual_review"]) == (
        2,
        1,
        1,
    )
    assert q["coverage"][0]["intervals"] == []
    with session() as s:
        assert s.get(Transaction, "tx_unnamed0").category_status == "needs_review"


def test_review_priority_cursor_currency_and_verified_exclusion(client, owner_headers, payment):
    with session() as s, s.begin():
        for i, (name, amount, currency) in enumerate(
            (
                ("Small", -100, "GBP"),
                ("Small", -100, "GBP"),
                ("Large", -9999, "GBP"),
                ("Foreign", -999999, "USD"),
                ("Verified", -99999, "GBP"),
            )
        ):
            tx = ingest(
                s,
                "acc_test",
                {
                    **payment,
                    "id": f"tx_{i}",
                    "merchant": {"id": "merch_" + name, "name": name},
                    "amount": amount,
                    "currency": currency,
                },
            )
            tx.category_status, tx.category_source = (
                ("assigned", "manual") if name == "Verified" else ("needs_review", "review")
            )
    base = "/v1/dashboard/review-merchants"
    q = {"tab": "needs_review", "sort": "value", "limit": 1}
    first = client.get(base, params=q, headers=owner_headers).json()
    assert first["items"][0]["name"] == "Large"
    assert first["items"][0]["debits_minor"] == 9999
    second = client.get(
        base, params={**q, "cursor": first["next_cursor"]}, headers=owner_headers
    ).json()
    assert second["items"][0]["name"] == "Small"
    assert second["next_cursor"] is None
    assert (
        client.get(
            base,
            params={**q, "sort": "frequency", "cursor": first["next_cursor"]},
            headers=owner_headers,
        ).status_code
        == 422
    )
    frequent = client.get(base, params={**q, "sort": "frequency"}, headers=owner_headers).json()
    assert frequent["items"][0]["name"] == "Small"
    assert frequent["items"][0]["debits_minor"] is None
