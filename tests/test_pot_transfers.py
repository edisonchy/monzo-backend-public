from sqlalchemy import func, select

from monzo_backend.backfill_pot_transfers import backfill
from monzo_backend.classifier import classify
from monzo_backend.db import session
from monzo_backend.domain import claim, finish, ingest
from monzo_backend.models import Account, Audit, Job, Transaction
from monzo_backend.spending import bank_facts

SCOPE = {"account_id": "combined", "period": "2026-10", "currency": "GBP"}


def pot(payment, **extra):
    return {
        **payment,
        "merchant": None,
        "settled": payment["created"],
        "scheme": "uk_retail_pot",
        "metadata": {"pot_id": "pot_test", "pot_account_id": "acc_savings"},
        **extra,
    }


def test_pots_resolve_without_jobs_or_provider_and_keep_raw_data(payment, monkeypatch):
    monkeypatch.setattr("monzo_backend.classifier.request", lambda *a, **kw: pytest_fail())
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", pot(payment))
        version = tx.version
        assert (tx.category, tx.category_source, tx.category_status) == (
            "transfers",
            "monzo",
            "assigned",
        )
        assert tx.merchant_name == ""
        assert tx.description == payment["description"]
        assert s.scalar(select(func.count()).select_from(Job)) == 0
        ingest(s, "acc_test", pot(payment))
        assert tx.version == version
        assert s.scalar(select(func.count()).select_from(Audit)) == 1
    assert classify(
        {"payload": {"transaction_id": payment["id"], "version": version}, "recovering": False}
    ) == {
        "category": "transfers",
        "source": "monzo",
        "status": "assigned",
    }


def pytest_fail():
    raise AssertionError("Pot categorisation must not request a provider")


def test_pot_names_reports_search_and_review(client, owner_headers, payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", pot(payment, amount=-500))
        ingest(s, "acc_test", pot(payment, id="withdrawal", amount=200))
        ingest(s, "acc_test", {**payment, "id": "ordinary", "merchant": None})
    response = client.get("/v1/dashboard/transactions", params=SCOPE, headers=owner_headers)
    assert response.status_code == 200
    rows = {x["id"]: x for x in response.json()["items"]}
    assert rows[payment["id"]]["merchant"] == "Transfer to savings"
    assert rows["withdrawal"]["merchant"] == "Transfer from savings"
    assert rows["ordinary"]["merchant"] == payment["description"]
    assert rows[payment["id"]]["category_status"] == "assigned"
    detail = client.get(f"/v1/dashboard/transactions/{payment['id']}", headers=owner_headers).json()
    assert detail["merchant"] == "Transfer to savings"
    search = client.get(
        "/v1/dashboard/transactions",
        params={**SCOPE, "q": "Transfer to savings"},
        headers=owner_headers,
    ).json()
    assert search["total"] == 1
    review = client.get(
        "/v1/dashboard/transactions",
        params={**SCOPE, "status": "needs_review"},
        headers=owner_headers,
    ).json()
    assert review["total"] == 0
    combined = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
    assert combined["current"]["debits_minor"] == combined["current"]["credits_minor"] == 0
    assert combined["current"]["excluded_count"] == 2
    retail = {**SCOPE, "account_id": "acc_test"}
    gross = client.get("/v1/dashboard/report", params=retail, headers=owner_headers).json()
    assert gross["current"]["debits_minor"] == 500
    assert gross["current"]["credits_minor"] == 200
    assert gross["merchants"] == [
        {"key": "Transfer to savings", "count": 1, "amount_minor": 500, "needs_review_count": 0}
    ]
    drilldown = client.get(
        "/v1/dashboard/transactions",
        params={**retail, "merchant": "Transfer to savings", "spending_only": True},
        headers=owner_headers,
    ).json()
    assert drilldown["total"] == 1
    assert drilldown["items"][0]["id"] == payment["id"]


def test_backfill_idempotent_manual_safe_and_stale_worker_fenced(payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", payment)
        tx.reporting_facts = bank_facts(pot(payment))
        tx.category_status = "needs_review"
        manual = ingest(s, "acc_test", {**payment, "id": "manual"})
        manual.reporting_facts = tx.reporting_facts
        manual.category, manual.category_source, manual.category_status = (
            "gifts",
            "manual",
            "assigned",
        )
        version = tx.version
    with session() as s, s.begin():
        work = claim(s, "category")
    assert work["payload"]["transaction_id"] == payment["id"]
    assert backfill() == {"verified": 2, "updated": 1, "provider_requests": 0}
    assert backfill()["updated"] == 0
    with session() as s, s.begin():
        finish(s, work, {"category": "shopping", "source": "ai", "status": "needs_review"})
        tx = s.get(Transaction, payment["id"])
        assert (tx.category, tx.category_status, tx.version) == (
            "transfers",
            "assigned",
            version + 1,
        )
        assert s.get(Transaction, "manual").category == "gifts"


def test_evidence_gained_and_lost_during_sync(payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", payment)
        tx.category_status = "needs_review"
        ingest(s, "acc_test", pot(payment, merchant=payment["merchant"]))
        assert (tx.category, tx.category_status) == ("transfers", "assigned")
        version = tx.version
        ingest(s, "acc_test", payment)
        assert tx.version == version + 1
        assert tx.category is None
        assert tx.category_status == "pending"
        jobs = s.scalars(select(Job)).all()
        assert any(j.payload["version"] == tx.version for j in jobs)


def test_missing_evidence_wrong_account_and_declined_not_autoassigned(payment):
    with session() as s, s.begin():
        s.add(Account(id="flex", connection_id=1, kind="uk_monzo_flex"))
        s.flush()
        cases = [
            pot(payment, id="no_pot", metadata={"pot_account_id": "acc_savings"}),
            pot(payment, id="no_account", metadata={"pot_id": "pot_test"}),
            pot(payment, id="wrong_scheme", scheme="mastercard"),
            pot(payment, id="declined", decline_reason="OTHER"),
            pot(payment, id="flex", account_id="flex"),
        ]
        for data in cases:
            tx = ingest(s, data["account_id"], data)
            assert tx.category_status == "pending"
    assert backfill()["updated"] == 0
