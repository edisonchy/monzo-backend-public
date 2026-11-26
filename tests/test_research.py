import copy
import json

import httpx
import pytest
from sqlalchemy import select

from monzo_backend.classifier import classify
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.http import ServiceError
from monzo_backend.models import MerchantResearch

URL = "https://find-and-update.company-information.service.gov.uk/company/123"


def work(payment, txid="first", recovering=False):
    payment = copy.deepcopy(payment)
    payment["id"] = txid
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    return {"payload": {"transaction_id": txid, "version": 1}, "recovering": recovering}


def search_response(category="groceries", urls=None):
    urls = [URL] if urls is None else urls
    return {
        "status": "completed",
        "output": [
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "sources": [{"url": URL}]},
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "category": category,
                                "needs_review": category is None,
                                "summary": "Food retailer" if category else "No reliable match",
                                "source_urls": urls,
                            }
                        ),
                        "annotations": [],
                    }
                ],
            },
        ],
        "usage": {"input_tokens": 800, "output_tokens": 100, "total_tokens": 900},
    }


@pytest.fixture
def routes(monkeypatch, respx_mock):
    monkeypatch.setenv("MONZO_AI_ENABLED", "true")
    monkeypatch.setenv("MONZO_WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("MONZO_MODEL", "test-model")
    monkeypatch.setenv("MONZO_PROXY_KEY", "test-key")
    settings.cache_clear()
    chat = respx_mock.post("http://cliproxyapi:8317/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"category":null,"needs_review":true}'}}],
            },
        )
    )
    search = respx_mock.post("http://cliproxyapi:8317/v1/responses").mock(
        return_value=httpx.Response(200, json=search_response())
    )
    return chat, search


def test_unknown_merchant_researched_once_across_model_and_bank_changes(
    payment, routes, monkeypatch, client, owner_headers, automation_headers
):
    chat, search = routes
    first = classify(work(payment))
    assert first["category"] == "groceries" and first["status"] == "needs_review"
    assert first["search_usage"]["total_tokens"] == 900
    monkeypatch.setenv("MONZO_MODEL", "changed-model")
    settings.cache_clear()
    payment["category"] = "category_custom"
    second = classify(work(payment, "second"))
    assert second["cache_hit"] and second["web_researched"]
    assert chat.call_count == search.call_count == 1
    with session() as s:
        row = s.scalar(select(MerchantResearch))
        assert row.finding["source_urls"] == [URL]
    assert client.get("/v1/merchant-research", headers=automation_headers).status_code == 403
    findings = client.get("/v1/merchant-research", headers=owner_headers).json()["findings"]
    assert len(findings) == 1 and findings[0]["finding"]["source_urls"] == [URL]
    body = json.loads(search.calls[0].request.content)
    from monzo_backend.category_policy import RESEARCH_INSTRUCTIONS

    assert body["instructions"] == RESEARCH_INSTRUCTIONS
    assert body["tools"][0]["type"] == "web_search"
    assert body["input"] == payment["merchant"]["name"]
    assert payment["description"] not in json.dumps(body)
    assert "acc_test" not in json.dumps(body)


def test_existing_unknown_cache_can_be_enriched(payment, routes, monkeypatch):
    chat, search = routes
    monkeypatch.setenv("MONZO_WEB_SEARCH_ENABLED", "false")
    settings.cache_clear()
    classify(work(payment))
    assert search.call_count == 0
    monkeypatch.setenv("MONZO_WEB_SEARCH_ENABLED", "true")
    settings.cache_clear()
    result = classify(work(payment, "second"))
    assert result["web_researched"]
    assert chat.call_count == search.call_count == 1


def test_negative_search_is_saved_without_researching(payment, routes):
    chat, search = routes
    search.mock(return_value=httpx.Response(200, json=search_response(None, [])))
    first = classify(work(payment))
    second = classify(work(payment, "second"))
    assert first["category"] is None and second["category"] is None
    assert second["cache_hit"]
    assert search.call_count == 1


@pytest.mark.parametrize(
    "bad", ["no_search", "incomplete", "invented_url", "no_sources", "bad_category"]
)
def test_search_evidence_validation(payment, routes, bad):
    _, search = routes
    data = search_response()
    if bad == "no_search":
        data["output"] = data["output"][1:]
    elif bad == "incomplete":
        data["status"] = "incomplete"
    elif bad == "invented_url":
        data = search_response(urls=["https://invented.example/"])
    elif bad == "no_sources":
        data = search_response(urls=[])
    else:
        data = search_response(category="invented")
    search.mock(return_value=httpx.Response(200, json=data))
    with pytest.raises(ServiceError, match="ai_search_invalid_evidence"):
        classify(work(payment))
    with session() as s:
        assert s.scalar(select(MerchantResearch)) is None


def test_search_failure_is_not_saved_as_no_match(payment, routes):
    _, search = routes
    search.mock(return_value=httpx.Response(503))
    with pytest.raises(ServiceError, match="provider_http_503"):
        classify(work(payment))
    with session() as s:
        assert s.scalar(select(MerchantResearch)) is None


def test_confident_classification_needs_no_search(payment, routes):
    chat, search = routes
    chat.mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"category":"shopping","needs_review":false}'}}
                ]
            },
        )
    )
    classify(work(payment))
    assert search.call_count == 0


def test_recovery_probes_provider_but_does_not_repeat_saved_search(payment, routes):
    chat, search = routes
    classify(work(payment))
    result = classify(work(payment, "second", recovering=True))
    assert result["provider_verified"]
    assert chat.call_count == 2 and search.call_count == 1


def test_manual_override_precedes_research(payment, routes):
    from monzo_backend.models import Transaction

    chat, search = routes
    classify(work(payment))
    item = work(payment, "second")
    with session() as s, s.begin():
        tx = s.get(Transaction, "second")
        tx.category_source, tx.category = "manual", "gifts"
    assert classify(item)["category"] == "gifts"
    assert chat.call_count == search.call_count == 1


def test_group_research_reused_across_name_variants(payment, routes, client, owner_headers):
    chat, search = routes
    classify(work(payment))
    payment["merchant"] = {"id": "second-identity", "name": " TEST SHOP "}
    payment["category"] = "category_second"
    result = classify(work(payment, "second"))
    assert result["cache_hit"] and result["web_researched"]
    assert chat.call_count == search.call_count == 1
    for txid in ("first", "second"):
        response = client.get(f"/v1/dashboard/transactions/{txid}", headers=owner_headers)
        assert response.status_code == 200
        saved = response.json()["research"]
        assert saved["finding"]["source_urls"] == [URL]
        assert saved["model"] == "test-model" and saved["created_at"]
    assert chat.call_count == search.call_count == 1


@pytest.mark.parametrize("grouped", [False, True])
def test_payment_research_keeps_legacy_name_fallback(payment, client, owner_headers, grouped):
    from monzo_backend.research import merchant_key

    if not grouped:
        payment["merchant"]["id"] = None
    work(payment)
    finding = {"category": None, "needs_review": True, "summary": "No match", "source_urls": []}
    with session() as s, s.begin():
        s.add(
            MerchantResearch(
                key=merchant_key("Test Shop"),
                merchant_name="Test Shop",
                finding=finding,
                model="legacy-model",
            )
        )
    response = client.get("/v1/dashboard/transactions/first", headers=owner_headers)
    assert response.status_code == 200
    assert response.json()["research"]["finding"] == finding


def test_payment_research_uses_matching_context_and_prefers_group(
    payment, routes, client, owner_headers
):
    from monzo_backend.research import merchant_key

    classify(work(payment))
    with session() as s, s.begin():
        s.add(
            MerchantResearch(
                key=merchant_key("Test Shop"),
                merchant_name="Test Shop",
                finding={"category": "health", "needs_review": False},
                model="legacy",
            )
        )
    response = client.get("/v1/dashboard/transactions/first", headers=owner_headers)
    assert response.json()["research"]["finding"]["category"] == "groceries"
    with session() as s, s.begin():
        s.delete(s.get(MerchantResearch, merchant_key("Test Shop")))
    payment["category"] = "eating_out"
    work(payment, "other-context")
    response = client.get("/v1/dashboard/transactions/other-context", headers=owner_headers)
    assert response.status_code == 200
    assert response.json()["research"] is None
    assert routes[0].call_count == routes[1].call_count == 1


def test_payment_research_rejects_conflicting_legacy_aliases(payment, client, owner_headers):
    from monzo_backend.research import merchant_key

    work(payment)
    payment["merchant"] = {"id": "second-identity", "name": " TEST SHOP "}
    work(payment, "second")
    with session() as s, s.begin():
        for name, category in (("Test Shop", "groceries"), (" TEST SHOP ", "health")):
            s.add(
                MerchantResearch(
                    key=merchant_key(name),
                    merchant_name=name,
                    finding={"category": category, "needs_review": False},
                    model="legacy",
                )
            )
    response = client.get("/v1/dashboard/transactions/first", headers=owner_headers)
    assert response.status_code == 200
    assert response.json()["research"] is None


@pytest.mark.parametrize("old_category", [None, "health"])
def test_legacy_research_cannot_bypass_current_policy(
    payment, routes, client, owner_headers, old_category
):
    from monzo_backend.classifier import cache_keys
    from monzo_backend.models import Transaction

    item = work(payment)
    old_finding = {
        "category": old_category,
        "needs_review": old_category is None,
        "summary": "Legacy evidence",
        "source_urls": [URL] if old_category else [],
    }
    with session() as s, s.begin():
        tx = s.get(Transaction, "first")
        legacy_key = cache_keys(s, tx, settings())[1][0]
        s.add(
            MerchantResearch(
                key=legacy_key,
                merchant_name=tx.merchant_name,
                finding=old_finding,
                model="legacy-model",
            )
        )
    assert (
        client.get("/v1/dashboard/transactions/first", headers=owner_headers).json()["research"][
            "finding"
        ]
        == old_finding
    )
    first = classify(item)
    second = classify(work(payment, "second"))
    assert first["category"] == second["category"] == "groceries"
    assert not first["cache_hit"] and second["cache_hit"]
    assert routes[0].call_count == routes[1].call_count == 1
    with session() as s:
        assert s.get(MerchantResearch, legacy_key).finding == old_finding
        assert len(s.scalars(select(MerchantResearch)).all()) == 2
    assert (
        client.get("/v1/dashboard/transactions/first", headers=owner_headers).json()["research"][
            "finding"
        ]["category"]
        == "groceries"
    )


def test_research_policy_refresh_is_once_and_preserves_old_evidence(
    payment, routes, monkeypatch, client, owner_headers
):
    from monzo_backend import category_policy
    from monzo_backend.models import Job, Transaction

    old_version = category_policy.RESEARCH_POLICY_VERSION
    first = classify(work(payment))
    with session() as s:
        old_keys = set(s.scalars(select(MerchantResearch.key)).all())
        old_jobs = [(row.id, row.state) for row in s.scalars(select(Job)).all()]
        original = s.get(Transaction, "first")
        old_state = (original.version, original.category, original.category_source)
    monkeypatch.setattr(category_policy, "RESEARCH_POLICY_VERSION", "research-test-next")
    monkeypatch.setattr(category_policy, "PREVIOUS_RESEARCH_POLICY_VERSIONS", (old_version,))
    assert (
        client.get("/v1/dashboard/transactions/first", headers=owner_headers).json()["research"][
            "finding"
        ]["category"]
        == first["category"]
    )
    with session() as s:
        assert [(row.id, row.state) for row in s.scalars(select(Job)).all()] == old_jobs
        original = s.get(Transaction, "first")
        assert (original.version, original.category, original.category_source) == old_state
    second = classify(work(payment, "second"))
    third = classify(work(payment, "third"))
    assert not second["cache_hit"] and third["cache_hit"]
    assert routes[0].call_count == 1
    assert routes[1].call_count == 2
    with session() as s:
        keys = set(s.scalars(select(MerchantResearch.key)).all())
        assert old_keys < keys and len(keys) == 2


def test_failed_policy_refresh_retains_prior_research(
    payment, routes, monkeypatch, client, owner_headers
):
    from monzo_backend import category_policy

    old_version = category_policy.RESEARCH_POLICY_VERSION
    classify(work(payment))
    monkeypatch.setattr(category_policy, "RESEARCH_POLICY_VERSION", "research-test-next")
    monkeypatch.setattr(category_policy, "PREVIOUS_RESEARCH_POLICY_VERSIONS", (old_version,))
    routes[1].mock(return_value=httpx.Response(503))
    with pytest.raises(ServiceError, match="provider_http_503"):
        classify(work(payment, "second"))
    with session() as s:
        assert len(s.scalars(select(MerchantResearch)).all()) == 1
    response = client.get("/v1/dashboard/transactions/first", headers=owner_headers)
    assert response.json()["research"]["finding"]["category"] == "groceries"


def test_legacy_payment_group_research_remains_visible(payment, routes, client, owner_headers):
    import hashlib

    from monzo_backend.classifier import cache_keys
    from monzo_backend.domain import CATEGORIES
    from monzo_backend.models import Transaction

    work(payment)
    finding = {
        "category": "health",
        "needs_review": True,
        "summary": "Legacy",
        "source_urls": [URL],
    }
    cfg = settings()
    with session() as s, s.begin():
        tx = s.get(Transaction, "first")
        tx.payment_group_identity = tx.merchant_id
        legacy_key = hashlib.sha256(
            json.dumps(
                [
                    "payment-group-v1",
                    tx.merchant_id,
                    "unknown",
                    cfg.model,
                    cfg.proxy_url,
                    "merchant-v1",
                    CATEGORIES,
                ]
            ).encode()
        ).hexdigest()
        assert cache_keys(s, tx, cfg)[1] == [legacy_key]
        assert cache_keys(s, tx, cfg)[0] != [legacy_key]
        s.add(
            MerchantResearch(
                key=legacy_key, merchant_name=tx.merchant_name, finding=finding, model=cfg.model
            )
        )
    response = client.get("/v1/dashboard/transactions/first", headers=owner_headers)
    assert response.json()["research"]["finding"] == finding
    assert routes[0].call_count == routes[1].call_count == 0


def test_current_research_aliases_do_not_conflict_with_old_aliases(
    payment, routes, client, owner_headers
):
    from monzo_backend.classifier import current_research_keys
    from monzo_backend.research import merchant_key

    work(payment)
    payment["merchant"] = {"id": "other-id", "name": "TEST SHOP"}
    work(payment, "second")
    with session() as s, s.begin():
        for key, name, category in (
            (current_research_keys([merchant_key("Test Shop")])[0], "Test Shop", "health"),
            (merchant_key("TEST SHOP"), "TEST SHOP", "shopping"),
        ):
            s.add(
                MerchantResearch(
                    key=key,
                    merchant_name=name,
                    finding={"category": category, "needs_review": True},
                    model="test-model",
                )
            )
    response = client.get("/v1/dashboard/transactions/first", headers=owner_headers)
    assert response.json()["research"]["finding"]["category"] == "health"
    assert routes[0].call_count == routes[1].call_count == 0


def test_conflicting_current_research_does_not_fall_back_to_legacy(
    payment, routes, client, owner_headers
):
    from monzo_backend.classifier import current_research_keys
    from monzo_backend.research import merchant_key

    work(payment)
    payment["merchant"] = {"id": "other-id", "name": "TEST SHOP"}
    work(payment, "second")
    with session() as s, s.begin():
        for key, name, category in (
            (current_research_keys([merchant_key("Test Shop")])[0], "Test Shop", "health"),
            (current_research_keys([merchant_key("TEST SHOP")])[0], "TEST SHOP", "shopping"),
            (merchant_key("Test Shop"), "Test Shop", "groceries"),
        ):
            s.add(
                MerchantResearch(
                    key=key,
                    merchant_name=name,
                    finding={"category": category, "needs_review": True},
                    model="test-model",
                )
            )
    response = client.get("/v1/dashboard/transactions/first", headers=owner_headers)
    assert response.json()["research"] is None
    assert routes[0].call_count == routes[1].call_count == 0
