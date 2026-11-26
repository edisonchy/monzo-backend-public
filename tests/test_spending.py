from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from monzo_backend.backfill_reporting import backfill
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import Account, Job, Transaction
from monzo_backend.spending import apply_bank_facts, bank_facts, exclusions

SCOPE = {"account_id": "combined", "period": "2026-10", "currency": "GBP"}


def add(s, payment, tx_id, amount, account="acc_test", **extra):
    data = {
        **payment,
        "id": tx_id,
        "account_id": account,
        "amount": amount,
        "settled": payment["created"],
        "scheme": "mastercard",
        **extra,
    }
    return ingest(s, account, data)


def flex_accounts(s):
    s.add(Account(id="flex", connection_id=1, kind="uk_monzo_flex", history_status="imported"))
    s.flush()


def pair(s, payment, ref="event", amount=2000, **extra):
    metadata = {
        "ledger_insertion_id": ref,
        "monzo_flex_id": "facility",
        "flex_transaction_type": "repayment",
    }
    a = add(s, payment, ref + "-out", -amount, scheme="monzo_flex", metadata=metadata, **extra)
    b = add(
        s, payment, ref + "-in", amount, "flex", scheme="monzo_flex", metadata=metadata, **extra
    )
    return a, b


def test_combined_exclusions_reconcile_all_views_and_preserve_gross_accounts(
    client, owner_headers, payment
):
    with session() as s, s.begin():
        flex_accounts(s)
        add(s, payment, "shop", -1000)
        add(s, payment, "flex-shop", -3000, "flex")
        pair(s, payment)
        add(s, payment, "refund", 100)
        add(
            s,
            payment,
            "pot",
            -500,
            scheme="uk_retail_pot",
            metadata={"pot_id": "pot_1", "pot_account_id": "acc_pot"},
        )
        add(s, payment, "named-only", -700, description="Flex repayment", category="transfers")
        add(
            s,
            payment,
            "unmatched",
            -800,
            scheme="monzo_flex",
            metadata={"flex_transaction_type": "repayment"},
        )
        add(s, payment, "pending", -900, settled="")
        add(s, payment, "declined", -950, decline_reason="OTHER")
    report = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
    assert report["combined"] is True
    assert report["current"]["debits_minor"] == 5500
    assert report["current"]["credits_minor"] == 100
    assert report["current"]["excluded_debits_minor"] == 2500
    assert report["current"]["excluded_count"] == 3
    assert report["current"]["unmatched_flex_count"] == 1
    assert report["current"]["evidence_missing_count"] == 0
    assert sum(report["current"]["series"]) == 5500
    for key in ["categories", "merchants"]:
        assert sum(x["amount_minor"] for x in report[key]) == 5500
    payments = client.get(
        "/v1/dashboard/transactions", params={**SCOPE, "spending_only": True}, headers=owner_headers
    ).json()
    assert sum(-x["amount_minor"] for x in payments["items"]) == 5500
    all_rows = client.get("/v1/dashboard/transactions", params=SCOPE, headers=owner_headers).json()
    assert all_rows["total"] == 10
    repayment = next(x for x in all_rows["items"] if x["id"] == "event-out")
    assert repayment["combined_exclusion"] == {
        "reason": "flex_repayment",
        "matched_transaction_id": "event-in",
    }
    detail = client.get("/v1/dashboard/transactions/event-out", headers=owner_headers).json()
    assert detail["combined_exclusion"] == repayment["combined_exclusion"]
    retail = client.get(
        "/v1/dashboard/report", params={**SCOPE, "account_id": "acc_test"}, headers=owner_headers
    ).json()
    assert retail["current"]["debits_minor"] == 5000
    assert retail["current"]["excluded_count"] == 0
    assert retail["combined"] is False


def test_matching_crosses_period_boundary_and_ignores_ai_category(client, owner_headers, payment):
    with session() as s, s.begin():
        flex_accounts(s)
        a, b = pair(s, payment)
        a.created_at = datetime(2026, 9, 30, 22, 59, 59, tzinfo=UTC)
        b.created_at = datetime(2026, 9, 30, 23, 0, 1, tzinfo=UTC)
        a.category = "shopping"
        a.category_source = "manual"
        a.category_status = "assigned"
        s.flush()
        assert len(exclusions(s)) == 2
    r = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
    assert r["previous"]["debits_minor"] == 0
    assert r["previous"]["excluded_debits_minor"] == 2000
    assert r["current"]["credits_minor"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "amount",
        "currency",
        "ledger",
        "facility",
        "type",
        "date",
        "pending",
        "declined",
        "duplicate",
        "kind",
    ],
)
def test_no_guessing_when_pair_evidence_disagrees(payment, change):
    with session() as s, s.begin():
        flex_accounts(s)
        a, b = pair(s, payment)
        if change == "amount":
            b.amount = 1999
        if change == "currency":
            b.currency = "EUR"
        if change in ["ledger", "facility", "type"]:
            key = {"ledger": "ledger_ref", "facility": "flex_ref", "type": "flex_type"}[change]
            b.reporting_facts = {**b.reporting_facts, key: "different"}
        if change == "date":
            b.created_at = datetime(2026, 10, 17, tzinfo=UTC)
        if change == "pending":
            b.settled_at = None
        if change == "declined":
            b.declined = True
        if change == "duplicate":
            c = add(s, payment, "duplicate", 2000, "flex")
            c.reporting_facts = dict(b.reporting_facts)
        if change == "kind":
            s.get(Account, "flex").kind = "uk_rewards"
        s.flush()
        assert exclusions(s) == {}


def test_rewards_and_unknown_accounts_excluded_on_every_dashboard_route(
    client, owner_headers, payment
):
    with session() as s, s.begin():
        flex_accounts(s)
        for kind in ["uk_rewards", "unknown", "uk_joint"]:
            s.add(Account(id=kind, connection_id=1, kind=kind))
            s.flush()
            add(s, payment, kind, -9999, kind)
        add(s, payment, "retail", -100)
        add(s, payment, "flex-card", -200, "flex")
    data = client.get("/v1/dashboard/bootstrap", headers=owner_headers).json()
    assert {a["id"] for a in data["accounts"]} == {"acc_test", "flex"}
    assert data["default_account_id"] == "combined"
    assert (
        client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()["current"][
            "debits_minor"
        ]
        == 300
    )
    for kind in ["uk_rewards", "unknown", "uk_joint"]:
        for path in ["report", "transactions"]:
            assert (
                client.get(
                    "/v1/dashboard/" + path,
                    params={**SCOPE, "account_id": kind},
                    headers=owner_headers,
                ).status_code
                == 404
            )
        assert (
            client.get("/v1/dashboard/transactions/" + kind, headers=owner_headers).status_code
            == 404
        )
        assert (
            client.put(
                "/v1/dashboard/transactions/" + kind + "/approve",
                headers=owner_headers,
                json={"category": "shopping", "expected_version": 1},
            ).status_code
            == 404
        )
    with session() as s, s.begin():
        assert s.get(Transaction, "uk_rewards").category_status == "pending"
        s.get(Account, "acc_test").kind = "uk_rewards"
        s.get(Account, "flex").kind = "unknown"
    data = client.get("/v1/dashboard/bootstrap", headers=owner_headers).json()
    assert data["accounts"] == [] and data["default_account_id"] is None


def test_minimal_evidence_backfill_does_not_reclassify_or_enqueue(monkeypatch, payment):
    data = {
        **payment,
        "scheme": "mastercard",
        "metadata": {"secret": "omit", "ledger_insertion_id": "bank-ref"},
    }
    facts = bank_facts(data)
    assert set(facts) == {"scheme", "ledger_ref"} and len(facts["ledger_ref"]) == 64
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", payment)
        tx.reporting_facts = None
        tx.reporting_checked_at = None
        tx.category = "shopping"
        tx.category_status = "assigned"
        tx.category_source = "manual"
        before = s.scalar(select(func.count()).select_from(Job))
    monkeypatch.setattr(
        "monzo_backend.backfill_reporting.bank_get", lambda path: {"transaction": data}
    )
    assert backfill(100, delay=0) == {"selected": 1, "updated": 1, "skipped": 0}
    assert backfill(100, delay=0)["selected"] == 0
    with session() as s:
        tx = s.get(Transaction, payment["id"])
        assert (tx.category, tx.category_source, tx.version) == ("shopping", "manual", 1)
        assert tx.reporting_facts == facts
        assert s.scalar(select(func.count()).select_from(Job)) == before
        with pytest.raises(ValueError):
            apply_bank_facts(tx, {**data, "amount": 999})
        with pytest.raises(ValueError):
            apply_bank_facts(tx, {**data, "account_id": "other"})


def test_combined_missing_evidence_and_currencies_are_explicit(client, owner_headers, payment):
    with session() as s, s.begin():
        tx = add(s, payment, "old", -100)
        tx.reporting_facts = None
        tx.reporting_checked_at = None
        add(s, payment, "euro", -999, currency="EUR")
        add(s, payment, "previous", -200, created="2026-09-15T11:00:00Z", scheme="")
    r = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
    assert r["current"]["debits_minor"] == 100 and r["current"]["evidence_missing_count"] == 1
    assert r["previous"]["debits_minor"] == 200 and r["previous"]["evidence_missing_count"] == 1
