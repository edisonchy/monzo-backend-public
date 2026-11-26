from datetime import timedelta

import pytest
from sqlalchemy import func, select

from monzo_backend import monzo
from monzo_backend.backfill_payment_identity import backfill
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import Job, MerchantIdentity, Transaction, now
from monzo_backend.payment_identity import PAYMENT_NAME, apply_identity, payment_name

SCOPE = {"account_id": "combined", "period": "2026-10", "currency": "GBP"}


def bank(payment, **kw):
    return {
        **payment,
        "merchant": None,
        "description": "123456",
        "settled": payment["created"],
        **kw,
    }


@pytest.mark.parametrize(
    "extra,expected,source",
    [
        ({"counterparty": {"name": "Example Payee"}}, "Example Payee", "counterparty"),
        ({"counterparty": {"preferred_name": "Preferred"}}, "Preferred", "counterparty"),
        (
            {"merchant": {"id": "m", "name": "Shop"}, "counterparty": {"name": "Payee"}},
            "Shop",
            "merchant",
        ),
        ({"description": "Extra", "scheme": "monzo_paid"}, "Monzo Extra", "description"),
        ({"description": "Refund from Monzo"}, "Refund from Monzo", "description"),
        ({}, "Payment reference: 123456", "reference"),
        ({"description": " \t "}, "Unnamed payment", None),
    ],
)
def test_identity_python_sql_and_sources(payment, extra, expected, source):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment, **extra), allow_ai=False)
        assert payment_name(tx) == expected
        assert tx.payment_name_source == source
        s.flush()
        assert s.scalar(select(PAYMENT_NAME).where(Transaction.id == tx.id)) == expected
        tx.payment_name = None  # Legacy fallback has the same labels when no counterparty exists.
        if source != "counterparty":
            s.flush()
            assert s.scalar(select(PAYMENT_NAME).where(Transaction.id == tx.id)) == payment_name(tx)


def test_sync_preserves_name_and_classification_and_no_group(payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment, counterparty={"name": "Payee"}), allow_ai=False)
        tx.category, tx.category_source, tx.category_status = "bills", "manual", "assigned"
        baseline = (tx.version, tx.input_hash, tx.category, tx.category_status, tx.category_source)
        ingest(s, "acc_test", bank(payment, counterparty=None, description=""))
        assert payment_name(tx) == "Payee"
        assert baseline == (
            tx.version,
            tx.input_hash,
            tx.category,
            tx.category_status,
            tx.category_source,
        )
        assert s.scalar(select(func.count()).select_from(MerchantIdentity)) == 0
        assert s.scalar(select(func.count()).select_from(Job)) == 0


def test_dashboard_search_report_quality(client, owner_headers, payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", bank(payment, counterparty={"name": "Example Payee"}), allow_ai=False)
    rows = client.get(
        "/v1/dashboard/transactions", params={**SCOPE, "q": "Example Payee"}, headers=owner_headers
    ).json()
    assert rows["total"] == 1
    assert rows["items"][0]["merchant"] == "Example Payee"
    report = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
    assert report["current"]["debits_minor"] == 1234
    assert report["merchants"][0]["key"] == "Example Payee"
    q = client.get("/v1/dashboard/quality", headers=owner_headers).json()
    assert q["needs_review"] == 1
    assert q["missing_payment_name"] == q["reference_only_name"] == 0


def test_backfill_atomic_idempotent_and_preserves_money_categories(payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment), allow_ai=False)
        tx.category, tx.category_source, tx.category_status = "shopping", "manual", "assigned"
        baseline = (tx.version, tx.input_hash, tx.amount, tx.reporting_facts, tx.category)
    data = bank(payment, counterparty={"name": "Found Payee"})
    with pytest.raises(ValueError):
        backfill(expected_count=2, apply=True, fetch=lambda _: data)
    with pytest.raises(ValueError):
        backfill(expected_count=1, apply=True, fetch=lambda _: {**data, "amount": -1})
    result = backfill(expected_count=1, apply=True, fetch=lambda _: data)
    assert result["changed"] == 1
    assert backfill(expected_count=1, apply=True, fetch=lambda _: data)["changed"] == 0
    with session() as s:
        tx = s.get(Transaction, payment["id"])
        assert payment_name(tx) == "Found Payee"
        assert baseline == (tx.version, tx.input_hash, tx.amount, tx.reporting_facts, tx.category)


def test_retry_bounded_and_metadata_only(payment, monkeypatch):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment), allow_ai=False)
        version = tx.version
    for attempt in range(3):
        with session() as s, s.begin():
            tx = s.get(Transaction, payment["id"])
            tx.identity_retry_after = now() - timedelta(seconds=1)
            monzo.schedule_sync(s, broad=True)
            assert tx.identity_attempts == attempt + 1
            monzo.schedule_sync(s, broad=True)
            assert tx.identity_attempts == attempt + 1
    with session() as s, s.begin():
        tx = s.get(Transaction, payment["id"])
        tx.identity_retry_after = now() - timedelta(days=1)
        monzo.schedule_sync(s, broad=True)
        assert tx.identity_attempts == 3
        assert (
            s.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "fetch_payment_identity")
            )
            == 3
        )
    monkeypatch.setattr(
        monzo,
        "bank_get",
        lambda *a: {"transaction": bank(payment, counterparty={"name": "Resolved"})},
    )
    monzo.execute(
        {
            "kind": "fetch_payment_identity",
            "payload": {"transaction_id": payment["id"], "account_id": "acc_test"},
        }
    )
    with session() as s:
        tx = s.get(Transaction, payment["id"])
        assert payment_name(tx) == "Resolved"
        assert tx.version == version


def test_backfill_rejects_wrong_identity(payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment), allow_ai=False)
        with pytest.raises(ValueError):
            apply_identity(tx, bank(payment, id="other"))


def test_mcp_detail_search_breakdown_and_noop_revision(payment):
    from monzo_backend.db import engine
    from monzo_backend.mcp.cursors import Cursors
    from monzo_backend.mcp.server import execute
    from monzo_backend.models import DashboardRevision

    data = bank(payment, counterparty={"name": "Bank Payee"})
    with session() as s, s.begin():
        ingest(s, "acc_test", data, allow_ai=False)
    with session() as s:
        revision = s.get(DashboardRevision, 1).revision
    with session() as s, s.begin():
        ingest(s, "acc_test", data, allow_ai=False)
    with session() as s:
        assert s.get(DashboardRevision, 1).revision == revision
    cursor = Cursors("identity-test-" + "x" * 32, "issuer|owner")
    scope = {"date_from": "2026-10-01", "date_to": "2026-10-16"}
    detail = execute("get_transaction", {"transaction_id": payment["id"]}, engine(), cursor)
    assert detail["payment"]["merchant"] == "Bank Payee"
    search = execute("search_transactions", {**scope, "query": "Bank Payee"}, engine(), cursor)
    assert search["matching_count"] == 1
    breakdown = execute(
        "get_breakdown", {**scope, "dimensions": ["merchant_group"]}, engine(), cursor
    )
    assert breakdown["rows"][0]["labels"] == ["Bank Payee"]


def test_incomplete_merchant_preserved_but_changed_identity_cleared(payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", payment, allow_ai=False)
        apply_identity(tx, {**payment, "merchant": None, "description": ""})
        assert payment_name(tx) == "Test Shop"
        apply_identity(
            tx, {**payment, "merchant": {"id": "different"}, "description": "Replacement"}
        )
        assert payment_name(tx) == "Replacement"


@pytest.mark.parametrize("description", ["Long bank description " * 100, "123456" * 300])
def test_long_description_display_stays_filterable(payment, client, owner_headers, description):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", bank(payment, description=description), allow_ai=False)
        name = payment_name(tx)
        assert len(name) == 500
        s.flush()
        assert s.scalar(select(PAYMENT_NAME).where(Transaction.id == tx.id)) == name
        tx.payment_name = None
        s.flush()
        assert s.scalar(select(PAYMENT_NAME).where(Transaction.id == tx.id)) == name
    response = client.get(
        "/v1/dashboard/transactions", params={**SCOPE, "merchant": name}, headers=owner_headers
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
