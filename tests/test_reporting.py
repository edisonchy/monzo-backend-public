from monzo_backend.db import session
from monzo_backend.domain import ingest


def test_cashflow_separates_currencies_and_excludes_pending_declined(
    payment, client, owner_headers
):
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "settled": "2026-10-16T01:00:00Z"})
        ingest(s, "acc_test", {**payment, "id": "tx_pending", "amount": -99999})
        ingest(
            s,
            "acc_test",
            {
                **payment,
                "id": "tx_declined",
                "amount": -99999,
                "decline_reason": "OTHER",
                "settled": "2026-10-16T01:00:00Z",
            },
        )
        ingest(
            s,
            "acc_test",
            {
                **payment,
                "id": "tx_eur",
                "currency": "EUR",
                "amount": 500,
                "settled": "2026-10-16T01:00:00Z",
            },
        )
    result = client.get(
        "/v1/cashflow",
        params={"start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
        headers=owner_headers,
    )
    assert result.status_code == 200
    groups = {r["currency"]: r for r in result.json()["groups"]}
    assert groups["GBP"]["debits_minor"] == 1234
    assert groups["GBP"]["count"] == 1
    assert groups["GBP"]["category"] == "uncategorised"
    assert groups["EUR"]["credits_minor"] == 500


def test_report_requires_timezone(client, owner_headers):
    result = client.get(
        "/v1/cashflow",
        params={"start": "2026-10-01T00:00:00", "end": "2026-11-01T00:00:00"},
        headers=owner_headers,
    )
    assert result.status_code == 422
