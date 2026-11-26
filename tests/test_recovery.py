from datetime import timedelta

from sqlalchemy import func, select

from monzo_backend.db import session
from monzo_backend.domain import claim, enqueue, finish, ingest, resume
from monzo_backend.http import ServiceError
from monzo_backend.models import Control, Incident, Job, Notification, Transaction, now


def take():
    with session() as s, s.begin():
        return claim(s, "category")


def test_pause_ingest_restart_resume(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    work = take()
    with session() as s, s.begin():
        finish(s, work, error=ServiceError("provider_http_401", False))
    with session() as s, s.begin():
        assert s.get(Control, 1).state == "paused"
        ingest(s, "acc_test", {**payment, "id": "tx_second"})
    assert take() is None
    # A fresh session represents the durable state seen after a process restart.
    with session() as s, s.begin():
        assert s.get(Control, 1).state == "paused"
        assert s.scalar(select(func.count()).select_from(Notification)) == 1
        assert resume(s) == "recovering"
        assert resume(s) == "recovering"
    retry = take()
    assert retry["id"] == work["id"]
    with session() as s, s.begin():
        assert (
            finish(s, work, result={"category": "shopping", "source": "ai", "status": "assigned"})
            is False
        )
        finish(
            s,
            retry,
            result={
                "category": "shopping",
                "source": "ai",
                "status": "assigned",
                "provider_verified": True,
            },
        )
    with session() as s:
        assert s.get(Control, 1).state == "running"
        assert s.scalar(select(Incident)).resolved_at is not None
        assert s.scalar(select(func.count()).select_from(Notification)) == 2
        assert s.scalar(select(func.count()).select_from(Transaction)) == 2
    assert take()["id"] != work["id"]


def test_failed_resume_stays_paused(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    work = take()
    with session() as s, s.begin():
        finish(s, work, error=ServiceError("invalid_output", False))
        resume(s)
    work = take()
    with session() as s, s.begin():
        finish(s, work, error=ServiceError("network_error"))
        assert s.get(Control, 1).state == "paused"
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Incident)) == 1
        assert s.scalar(select(func.count()).select_from(Notification)) == 2


def test_three_failures_block_later_jobs(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
        ingest(s, "acc_test", {**payment, "id": "tx_second"})
    original = None
    for _attempt in range(3):
        work = take()
        original = original or work["id"]
        assert work["id"] == original
        with session() as s, s.begin():
            finish(s, work, error=ServiceError("provider_network_error"))
            s.get(Job, work["id"]).available_at = now() - timedelta(seconds=1)
    assert take() is None
    with session() as s:
        assert s.get(Control, 1).state == "paused"


def test_expired_lease_fences_late_result(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    first = take()
    assert take() is None
    with session() as s, s.begin():
        s.get(Job, first["id"]).lease_until = now() - timedelta(seconds=1)
    second = take()
    assert first["id"] == second["id"]
    assert first["lease_token"] != second["lease_token"]
    with session() as s, s.begin():
        assert not finish(s, first, result={})
        finish(s, second, result={"category": None, "source": "review", "status": "needs_review"})
        assert s.get(Control, 1).state == "running"


def test_manual_correction_survives_inflight_result(payment, client, owner_headers):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    work = take()
    response = client.put(
        "/v1/transactions/tx_test/category",
        headers=owner_headers,
        json={"category": "groceries", "expected_version": 1},
    )
    assert response.status_code == 200
    with session() as s, s.begin():
        finish(s, work, result={"category": "shopping", "source": "ai", "status": "assigned"})
        ingest(s, "acc_test", {**payment, "category": "shopping"})
        assert s.get(Transaction, "tx_test").category == "groceries"


def test_duplicate_ingestion_one_job_and_transaction(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
        ingest(s, "acc_test", payment)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1
        assert s.scalar(select(func.count()).select_from(Job)) == 1


def test_bank_change_does_not_accept_stale_category(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    work = take()
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "merchant": {"id": "merch_new", "name": "New Shop"}})
        finish(s, work, result={"category": "shopping", "source": "ai", "status": "assigned"})
        assert s.get(Transaction, "tx_test").category is None
    assert take() is not None


def test_initial_history_outranks_routine_sync():
    with session() as s, s.begin():
        enqueue(s, "sync", "fetch_transaction", "old-routine", {})
        initial = enqueue(s, "sync", "sync_page", "initial-history", {"historical": True})
        assert claim(s, "sync")["id"] == initial


def test_manual_pause_can_resume_without_ai(payment, client, owner_headers):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    take()
    assert client.post("/v1/processing/pause", headers=owner_headers).status_code == 200
    assert client.post("/v1/processing/resume", headers=owner_headers).status_code == 200
    work = take()
    assert work["recovering"] is False
    with session() as s, s.begin():
        finish(s, work, result={"category": None, "source": "review", "status": "needs_review"})
        assert s.get(Control, 1).state == "running"


def test_stale_recovery_job_does_not_claim_provider_recovered(payment):
    from monzo_backend.classifier import classify

    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    work = take()
    with session() as s, s.begin():
        finish(s, work, error=ServiceError("provider_http_401", False))
        ingest(s, "acc_test", {**payment, "merchant": {"id": "new", "name": "New Shop"}})
        resume(s)
    stale = take()
    result = classify(stale)
    assert result == {"skipped": "stale_input"}
    with session() as s, s.begin():
        finish(s, stale, result=result)
        assert s.get(Control, 1).state == "recovering"
        assert s.scalar(select(Incident)).resolved_at is None
    next_job = take()
    assert next_job["recovering"]
    assert next_job["payload"]["version"] == 2


def test_manual_correction_keeps_last_provider_recovery_probe(
    payment, client, owner_headers, monkeypatch, respx_mock
):
    import httpx

    from monzo_backend.classifier import classify
    from monzo_backend.config import settings

    monkeypatch.setenv("MONZO_AI_ENABLED", "true")
    monkeypatch.setenv("MONZO_MODEL", "test-model")
    monkeypatch.setenv("MONZO_PROXY_KEY", "test-key")
    settings.cache_clear()
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", payment)
        version = tx.version
    work = take()
    with session() as s, s.begin():
        finish(s, work, error=ServiceError("provider_http_401", False))
    assert (
        client.put(
            "/v1/transactions/tx_test/category",
            headers=owner_headers,
            json={"category": "groceries", "expected_version": version},
        ).status_code
        == 200
    )
    route = respx_mock.post("http://cliproxyapi:8317/v1/chat/completions").mock(
        return_value=httpx.Response(401)
    )
    with session() as s, s.begin():
        resume(s)
    retry = take()
    try:
        classify(retry)
    except ServiceError as error:
        with session() as s, s.begin():
            finish(s, retry, error=error)
    else:
        raise AssertionError("Failed provider must not certify recovery")
    with session() as s, s.begin():
        assert s.get(Control, 1).state == "paused"
        assert s.get(Transaction, "tx_test").category == "groceries"
        resume(s)
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"category":"shopping","needs_review":false}'}}
                ]
            },
        )
    )
    retry = take()
    result = classify(retry)
    assert result["provider_verified"]
    assert route.call_count == 2
    with session() as s, s.begin():
        finish(s, retry, result=result)
        assert s.get(Control, 1).state == "running"
        assert s.scalar(select(Incident)).resolved_at is not None
        tx = s.get(Transaction, "tx_test")
        assert (tx.category, tx.category_source, tx.version) == ("groceries", "manual", version + 1)
        assert claim(s, "category") is None


def test_card_check_keeps_private_recovery_probe(
    payment, client, owner_headers, monkeypatch, respx_mock
):
    import json

    import httpx
    import pytest

    from monzo_backend.classifier import classify
    from monzo_backend.config import settings
    from monzo_backend.models import MerchantDecision

    monkeypatch.setenv("MONZO_AI_ENABLED", "true")
    monkeypatch.setenv("MONZO_MODEL", "test-model")
    monkeypatch.setenv("MONZO_PROXY_KEY", "test-key")
    settings.cache_clear()
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "amount": 0})
    work = take()
    with session() as s, s.begin():
        finish(s, work, error=ServiceError("provider_http_401", False))
    response = client.put(
        "/v1/dashboard/transactions/tx_test/card-verification",
        headers=owner_headers,
        json={"expected_version": 1},
    )
    assert response.status_code == 200, response.text
    route = respx_mock.post("http://cliproxyapi:8317/v1/chat/completions").mock(
        return_value=httpx.Response(401)
    )
    with session() as s, s.begin():
        assert resume(s) == "recovering"
    retry = take()
    with pytest.raises(ServiceError) as failed:
        classify(retry)
    with session() as s, s.begin():
        finish(s, retry, error=failed.value)
        assert s.get(Control, 1).state == "paused"
        assert s.scalar(select(Incident)).resolved_at is None
        assert resume(s) == "recovering"
    route.mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"category":null,"needs_review":true}'}}]},
        )
    )
    retry = take()
    result = classify(retry)
    assert result["provider_verified"]
    assert route.call_count == 2
    for call in route.calls:
        payload = json.loads(call.request.content)
        assert json.loads(payload["messages"][1]["content"]) == {
            "merchant": "",
            "bank_category": "unknown",
        }
    with session() as s, s.begin():
        finish(s, retry, result=result)
        assert s.get(Control, 1).state == "running"
        assert s.scalar(select(Incident)).resolved_at is not None
        tx = s.get(Transaction, "tx_test")
        assert (tx.category, tx.category_source, tx.category_status, tx.version) == (
            None,
            "owner_verification",
            "excluded",
            2,
        )
        assert s.scalar(select(MerchantDecision)) is None
        assert claim(s, "category") is None
