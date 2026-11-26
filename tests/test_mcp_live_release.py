from datetime import UTC, datetime

from sqlalchemy import select

from monzo_backend.db import engine, session
from monzo_backend.domain import ingest
from monzo_backend.mcp.cursors import Cursors
from monzo_backend.mcp.server import execute
from monzo_backend.models import ReportingCoverage, Transaction

FILTER = {
    "date_from": "2026-10-01",
    "date_to": "2026-10-31",
    "reported_spending_only": True,
    "query": "Test Shop",
}


def read(name, **params):
    return execute(name, params, engine(), Cursors("local-test-key", "fixture"))


def test_filtered_spending_and_pending_detail_after_settlement(payment):
    hold = dict(payment, id="hold", amount=-500, scheme="mastercard")
    with session() as db, db.begin():
        for row in (
            dict(payment, amount=-1200, settled="2026-10-16T00:00:00Z"),
            dict(
                payment,
                id="other",
                merchant={"id": "other", "name": "Other Shop"},
                settled="2026-10-16T00:00:00Z",
            ),
            hold,
        ):
            ingest(db, "acc_test", row)

    def summary():
        return read("search_transactions", **FILTER, mode="summary")

    result = summary()
    assert result["rows"] == [] and result["matching_count"] == 1
    assert result["summary"]["recorded_total_minor"] == 1200
    assert result["summary"]["pending_card_minor"] == 500
    assert result["summary"]["pending_card_count"] == 1
    settled = read("search_transactions", **FILTER, mode="payments", settlement="settled")
    assert [r["id"] for r in settled["rows"]] == [payment["id"]]
    pending = read("search_transactions", **FILTER, mode="payments", settlement="pending")
    (row,) = pending["rows"]
    assert row["id"] == "hold" and row["pending_card"] and row["amount_minor"] == -500
    assert read("get_transaction", transaction_id=row["id"])["payment"]["pending_card"]

    with session() as db, db.begin():
        ingest(db, "acc_test", dict(hold, amount=-480, settled="2026-10-16T00:00:00Z"))
    result = summary()
    assert result["matching_count"] == 2
    assert result["summary"]["recorded_total_minor"] == 1680
    assert result["summary"]["pending_card_minor"] == result["summary"]["pending_card_count"] == 0
    detail = read("get_transaction", transaction_id=row["id"])["payment"]
    assert detail["amount_minor"] == -480 and detail["settled_at"] and not detail["pending_card"]


def test_absent_hold_is_excluded_after_a_later_completed_bank_scan(payment):
    with session() as db, db.begin():
        ingest(db, "acc_test", dict(payment, scheme="mastercard"))
        tx = db.scalar(select(Transaction).where(Transaction.id == payment["id"]))
        tx.reporting_checked_at = datetime(2026, 10, 15, 12, tzinfo=UTC)
        db.add(
            ReportingCoverage(
                account_id="acc_test",
                start_at=datetime(2026, 10, 14, tzinfo=UTC),
                end_at=datetime(2026, 10, 16, tzinfo=UTC),
                source="monzo_recent_paginated",
            )
        )
    summary = read("search_transactions", **FILTER, mode="summary")["summary"]
    assert summary["recorded_total_minor"] == summary["pending_card_minor"] == 0
    assert read("search_transactions", **FILTER, settlement="pending")["rows"] == []
    assert not read("get_transaction", transaction_id=payment["id"])["payment"]["pending_card"]
