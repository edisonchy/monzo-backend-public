import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from monzo_backend.category_policy import classification_request
from monzo_backend.classifier import classify
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import claim, ingest
from monzo_backend.http import ServiceError
from monzo_backend.models import Account, Connection, Job, Transaction, now
from monzo_backend.monzo import access_token, decrypt, encrypt, execute, save_tokens


def prepare(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
        return claim(s, "category")


def test_ai_sanitised_payload_and_suggestion_mode(payment, monkeypatch, respx_mock):
    monkeypatch.setenv("MONZO_AI_ENABLED", "true")
    monkeypatch.setenv("MONZO_MODEL", "test-model")
    monkeypatch.setenv("MONZO_PROXY_KEY", "test-api-key")
    settings.cache_clear()
    route = respx_mock.post("http://cliproxyapi:8317/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"category":"shopping","needs_review":false}'}}
                ]
            },
        )
    )
    result = classify(prepare(payment))
    assert result["category"] == "shopping"
    assert result["status"] == "needs_review"
    payload = json.loads(route.calls[0].request.content)
    assert payload == classification_request(
        payment["merchant"]["name"], payment["category"], "test-model"
    )
    assert payment["description"] not in json.dumps(payload)
    assert "-1234" not in json.dumps(payload)
    assert "acc_test" not in json.dumps(payload)


def test_ai_bad_category_rejected(payment, monkeypatch, respx_mock):
    monkeypatch.setenv("MONZO_AI_ENABLED", "true")
    monkeypatch.setenv("MONZO_MODEL", "test-model")
    monkeypatch.setenv("MONZO_PROXY_KEY", "test-api-key")
    settings.cache_clear()
    respx_mock.post("http://cliproxyapi:8317/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"category":"invented","needs_review":false}'}}
                ]
            },
        )
    )
    with pytest.raises(ServiceError, match="ai_invalid_output"):
        classify(prepare(payment))


def test_ai_disabled_does_not_call_provider(payment, respx_mock):
    assert classify(prepare(payment))["status"] == "needs_review"
    assert len(respx_mock.calls) == 0


def test_token_import_encrypts_and_verifies_identity(respx_mock):
    respx_mock.get("https://api.monzo.com/ping/whoami").mock(
        return_value=httpx.Response(200, json={"authenticated": True, "user_id": "user_test"})
    )
    save_tokens(
        {"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 1000},
        "user_test",
    )
    with session() as s:
        conn = s.get(Connection, 1)
        assert conn.access_token != "secret-access"
        assert decrypt(conn.refresh_token) == "secret-refresh"
        assert s.scalar(select(Job)).kind == "discover"


def test_token_import_rejects_wrong_user(respx_mock):
    respx_mock.get("https://api.monzo.com/ping/whoami").mock(
        return_value=httpx.Response(200, json={"authenticated": True, "user_id": "someone_else"})
    )
    with pytest.raises(ServiceError, match="monzo_user_mismatch"):
        save_tokens({"access_token": "wrong"}, "user_test")


def test_refresh_rotates_and_does_not_repeat(monkeypatch, respx_mock):
    monkeypatch.setenv("MONZO_CLIENT_ID", "client")
    monkeypatch.setenv("MONZO_CLIENT_SECRET", "secret")
    settings.cache_clear()
    with session() as s, s.begin():
        conn = s.get(Connection, 1)
        conn.access_token, conn.refresh_token = encrypt("old"), encrypt("refresh-old")
        conn.expires_at = now() - timedelta(seconds=1)
    route = respx_mock.post("https://api.monzo.com/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "new", "refresh_token": "refresh-new", "expires_in": 3600}
        )
    )
    assert access_token() == "new"
    assert access_token() == "new"
    assert route.call_count == 1
    with session() as s:
        assert decrypt(s.get(Connection, 1).refresh_token) == "refresh-new"


def test_unknown_refresh_outcome_requires_reauthorisation(monkeypatch, respx_mock):
    monkeypatch.setenv("MONZO_CLIENT_ID", "client")
    monkeypatch.setenv("MONZO_CLIENT_SECRET", "secret")
    settings.cache_clear()
    with session() as s, s.begin():
        conn = s.get(Connection, 1)
        conn.access_token, conn.refresh_token = encrypt("old"), encrypt("refresh")
    route = respx_mock.post("https://api.monzo.com/oauth2/token").mock(
        side_effect=httpx.ReadTimeout("secret must not leak")
    )
    with pytest.raises(ServiceError, match="monzo_reauthorisation_required"):
        access_token()
    with pytest.raises(ServiceError, match="monzo_reauthorisation_required"):
        access_token()
    assert route.call_count == 1


def test_sync_writes_authoritative_transaction_and_queue(payment, respx_mock):
    with session() as s, s.begin():
        conn = s.get(Connection, 1)
        conn.access_token, conn.expires_at = encrypt("access"), now() + timedelta(hours=1)
    respx_mock.get("https://api.monzo.com/transactions").mock(
        return_value=httpx.Response(200, json={"transactions": [payment]})
    )
    execute(
        {
            "id": "sync-test",
            "kind": "sync_page",
            "payload": {
                "account_id": "acc_test",
                "since": "2026-10-01T00:00:00Z",
                "before": "2026-10-16T00:00:00Z",
                "historical": False,
            },
        }
    )
    with session() as s:
        assert s.get(Transaction, "tx_test").amount == -1234
        assert s.scalar(select(Job)).queue == "category"


def test_interrupted_refresh_persists_reauthorisation_state(respx_mock):
    with session() as s, s.begin():
        s.get(Connection, 1).state = "refreshing"
    with pytest.raises(ServiceError, match="monzo_reauthorisation_required"):
        access_token()
    with session() as s:
        assert s.get(Connection, 1).state == "reauthorisation_required"
    assert len(respx_mock.calls) == 0


def test_discovery_starts_history_with_bounded_window(monkeypatch):
    created = now() - timedelta(days=400)
    monkeypatch.setattr(
        "monzo_backend.monzo.bank_get",
        lambda *_: {
            "accounts": [{"id": "acc_test", "type": "uk_retail", "created": created.isoformat()}]
        },
    )
    execute({"id": "discovery", "kind": "discover", "payload": {}})
    with session() as s:
        p = s.scalar(select(Job)).payload
        assert p["historical"]
        assert datetime.fromisoformat(p["since"]) == created - timedelta(seconds=1)
        assert datetime.fromisoformat(p["before"]) - datetime.fromisoformat(
            p["since"]
        ) == timedelta(days=30)
        assert datetime.fromisoformat(p["history_end"]) > datetime.fromisoformat(p["before"])


def test_history_only_completes_after_final_window(monkeypatch):
    monkeypatch.setattr("monzo_backend.monzo.bank_get", lambda *_: {"transactions": []})
    end = now()
    payload = {
        "account_id": "acc_test",
        "since": (end - timedelta(days=40)).isoformat(),
        "before": (end - timedelta(days=10)).isoformat(),
        "history_end": end.isoformat(),
        "historical": True,
    }
    with session() as s, s.begin():
        s.get(Account, "acc_test").history_status = "importing"
    execute({"id": "window-one", "kind": "sync_page", "payload": payload})
    with session() as s:
        acct = s.get(Account, "acc_test")
        assert acct.history_status == "importing" and acct.last_sync_at is None
        next_payload = s.scalar(select(Job)).payload
        assert datetime.fromisoformat(next_payload["since"]) == end - timedelta(days=10, seconds=1)
        assert next_payload["before"] == payload["history_end"]
    execute({"id": "window-two", "kind": "sync_page", "payload": next_payload})
    with session() as s:
        assert s.get(Account, "acc_test").history_status == "imported"
        assert s.get(Account, "acc_test").last_sync_at is not None


def test_full_history_page_keeps_window_and_uses_last_cursor(monkeypatch, payment):
    items = [{**payment, "id": f"tx_page{i}"} for i in range(100)]
    monkeypatch.setattr("monzo_backend.monzo.bank_get", lambda *_: {"transactions": items})
    end = now()
    payload = {
        "account_id": "acc_test",
        "since": (end - timedelta(days=40)).isoformat(),
        "before": (end - timedelta(days=10)).isoformat(),
        "history_end": end.isoformat(),
        "historical": True,
    }
    execute({"id": "full-page", "kind": "sync_page", "payload": payload})
    with session() as s:
        follow = s.scalar(select(Job).where(Job.queue == "sync")).payload
        assert follow == {**payload, "since": "tx_page99", "coverage_since": payload["since"]}


def test_expired_history_permission_falls_back_to_valid_recent_range(monkeypatch):
    def denied(*_):
        raise ServiceError("monzo_http_403", False)

    monkeypatch.setattr("monzo_backend.monzo.bank_get", denied)
    old = now() - timedelta(days=400)
    payload = {
        "account_id": "acc_test",
        "since": (old - timedelta(days=30)).isoformat(),
        "before": old.isoformat(),
        "history_end": now().isoformat(),
        "historical": True,
    }
    execute({"id": "restricted-window", "kind": "sync_page", "payload": payload})
    with session() as s:
        follow = s.scalar(select(Job)).payload
        assert not follow["historical"]
        assert datetime.fromisoformat(follow["before"]) - datetime.fromisoformat(
            follow["since"]
        ) == timedelta(days=89)
        assert follow["before"] == follow["history_end"]
        assert s.get(Account, "acc_test").history_status == "incomplete"
