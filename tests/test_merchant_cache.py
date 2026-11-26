import copy
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from monzo_backend.classifier import classify
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.http import ServiceError
from monzo_backend.models import MerchantDecision, Transaction, now


@pytest.fixture
def ai(monkeypatch, respx_mock):
    monkeypatch.setenv("MONZO_AI_ENABLED", "true")
    monkeypatch.setenv("MONZO_MODEL", "test-model")
    monkeypatch.setenv("MONZO_PROXY_KEY", "test-key")
    settings.cache_clear()
    return respx_mock.post("http://cliproxyapi:8317/v1/chat/completions")


def response(category="shopping", review=False):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps({"category": category, "needs_review": review})}}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 15,
                "total_tokens": 115,
                "untrusted_extra": "not stored",
            },
        },
    )


def work(payment, txid):
    payment = copy.deepcopy(payment)
    payment["id"] = txid
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    return {"payload": {"transaction_id": txid, "version": 1}, "recovering": False}


@pytest.mark.parametrize("category,review", [("shopping", False), (None, True), ("shopping", True)])
def test_repeated_merchant_reuses_valid_decision(payment, ai, category, review):
    ai.mock(return_value=response(category, review))
    first = classify(work(payment, "first"))
    second = classify(work(payment, "second"))
    assert ai.call_count == 1
    assert first["usage"] == {"prompt_tokens": 100, "completion_tokens": 15, "total_tokens": 115}
    assert "usage" not in second
    assert second["cache_hit"] and not second["provider_verified"]
    assert first["category"] == second["category"] == category
    assert second["status"] == "needs_review"


@pytest.mark.parametrize("change", ["merchant", "bank", "model", "policy", "recovery"])
def test_cache_boundaries_and_recovery(payment, ai, monkeypatch, change):
    ai.mock(return_value=response())
    classify(work(payment, "first"))
    if change == "merchant":
        payment["merchant"]["name"] += " different"
        payment["merchant"]["id"] = "different-merchant"
    elif change == "bank":
        payment["category"] = "income"
    elif change == "model":
        monkeypatch.setenv("MONZO_MODEL", "new-model")
        settings.cache_clear()
    elif change == "policy":
        monkeypatch.setattr("monzo_backend.classifier.POLICY_VERSION", "merchant-test-next")
    item = work(payment, "second")
    item["recovering"] = change == "recovery"
    result = classify(item)
    assert ai.call_count == 2
    assert not result["cache_hit"] and result["provider_verified"]


def test_redaction_does_not_merge_merchants(payment, ai):
    ai.mock(return_value=response())
    payment["merchant"]["name"] = "Shop 123456"
    classify(work(payment, "first"))
    payment["merchant"]["name"] = "Shop 654321"
    payment["merchant"]["id"] = "different-merchant"
    classify(work(payment, "second"))
    assert ai.call_count == 2


def test_invalid_output_is_not_cached(payment, ai):
    ai.mock(return_value=response("made-up"))
    with pytest.raises(ServiceError):
        classify(work(payment, "first"))
    with session() as s:
        assert s.scalar(select(MerchantDecision)) is None


def test_manual_override_precedes_cache(payment, ai):
    ai.mock(return_value=response())
    classify(work(payment, "first"))
    item = work(payment, "second")
    with session() as s, s.begin():
        tx = s.get(Transaction, "second")
        tx.category, tx.category_source = "gifts", "manual"
    result = classify(item)
    assert result["category"] == "gifts" and result["source"] == "manual"
    assert ai.call_count == 1


def test_backfill_skips_processed_and_unnamed(payment, client, owner_headers):
    for name, merchant, source in [
        ("new", "Shop", "review"),
        ("old", "Shop", "ai"),
        ("blank", "", "review"),
        ("manual", "Shop", "manual"),
    ]:
        payment["merchant"]["name"] = merchant
        work(payment, name)
        with session() as s, s.begin():
            tx = s.get(Transaction, name)
            tx.category_status, tx.category_source = "needs_review", source
    path = "/v1/reclassify?unprocessed_only=true"
    assert client.post(path, headers=owner_headers).json() == {"queued": 1}
    assert client.post(path, headers=owner_headers).json() == {"queued": 0}
    with session() as s, s.begin():
        tx = s.get(Transaction, "new")
        tx.category_status, tx.category_source = "needs_review", "ai"
    assert client.post(path, headers=owner_headers).json() == {"queued": 0}


def test_saved_decision_has_no_expiry(payment, ai, monkeypatch):
    ai.mock(return_value=response())
    classify(work(payment, "first"))
    with session() as s:
        assert s.scalar(select(MerchantDecision)).expires_at is None
    future = now() + timedelta(days=3650)
    monkeypatch.setattr("monzo_backend.classifier.now", lambda: future)
    result = classify(work(payment, "ten-years-later"))
    assert result["cache_hit"]
    assert result["status"] == "needs_review"
    assert ai.call_count == 1


def test_group_reuses_name_variants_and_opaque_bank_ids(payment, ai):
    ai.mock(return_value=response())
    payment["merchant"]["name"] = "Test Shop"
    payment["category"] = "category_first"
    classify(work(payment, "first"))
    payment["merchant"] = {"id": "another-bank-identity", "name": " TEST  SHOP "}
    payment["category"] = "category_second"
    result = classify(work(payment, "second"))
    assert result["cache_hit"] and ai.call_count == 1


def test_legacy_cache_reused_for_group_without_provider(payment, ai):
    from monzo_backend.classifier import decision_key

    item = work(payment, "first")
    with session() as s, s.begin():
        tx = s.get(Transaction, "first")
        s.add(
            MerchantDecision(
                key=decision_key(tx, settings()),
                decision={"category": "shopping", "needs_review": False},
            )
        )
    payment["merchant"] = {"id": "another", "name": "TEST SHOP"}
    payment["category"] = "category_other"
    result = classify(work(payment, "second"))
    assert result["cache_hit"] and ai.call_count == 0
    assert classify(item)["cache_hit"]


def test_stale_job_skips_provider_and_completes(payment, ai):
    from monzo_backend.domain import claim, finish
    from monzo_backend.models import Job

    work(payment, "first")
    with session() as s, s.begin():
        item = claim(s, "category")
        tx = s.get(Transaction, "first")
        tx.version += 1
        tx.category, tx.category_source = "gifts", "manual"
    result = classify(item)
    assert result == {"skipped": "stale_input"}
    assert ai.call_count == 0
    with session() as s, s.begin():
        finish(s, item, result=result)
        assert s.get(Job, item["id"]).state == "completed"
        assert s.get(Transaction, "first").category == "gifts"


def test_provider_requests_record_failures_and_usage(payment, ai):
    from monzo_backend.models import Audit

    item = work(payment, "first")
    ai.mock(return_value=httpx.Response(503))
    with pytest.raises(ServiceError):
        classify(item)
    ai.mock(return_value=response())
    classify(item)
    classify(work(payment, "second"))
    with session() as s:
        rows = s.scalars(
            select(Audit).where(Audit.action == "ai_provider_request").order_by(Audit.created_at)
        ).all()
        assert len(rows) == 2
        assert rows[0].detail["state"] == "failed"
        assert rows[0].detail["error_code"] == "provider_http_503"
        assert rows[1].detail["usage"]["total_tokens"] == 115
        assert "merchant" not in json.dumps([r.detail for r in rows])


def test_conflicting_legacy_suggestions_do_not_pick_arbitrarily(payment, ai):
    from monzo_backend.classifier import decision_key

    ai.mock(return_value=response("health"))
    for txid, name, category in [
        ("first", "Test Shop", "shopping"),
        ("second", "TEST SHOP", "gifts"),
    ]:
        payment["merchant"] = {"id": txid, "name": name}
        work(payment, txid)
        with session() as s, s.begin():
            s.add(
                MerchantDecision(
                    key=decision_key(s.get(Transaction, txid), settings()),
                    decision={"category": category, "needs_review": False},
                )
            )
    result = classify(work(payment, "third"))
    assert result["category"] == "health" and ai.call_count == 1
