from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from monzo_backend.api import app
from monzo_backend.browser_auth import COOKIE, digest, hash_password
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import Account, Audit, BrowserSession, Rule, Transaction, now

SCOPE = {"account_id": "acc_test", "period": "2026-10", "currency": "GBP"}
ORIGIN = "https://finance.example.test"
HEADERS = {"Origin": ORIGIN, "X-Dashboard-Request": "1"}


def configure_login(monkeypatch):
    monkeypatch.setenv("MONZO_DASHBOARD_ORIGIN", ORIGIN)
    monkeypatch.setenv("MONZO_DASHBOARD_PASSWORD_HASH", hash_password("test-password"))
    settings.cache_clear()


def test_browser_sessions_csrf_expiry_logout_and_rotation(monkeypatch):
    configure_login(monkeypatch)
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/dashboard/bootstrap").status_code == 401
        assert c.post("/dashboard/login", json={"password": "test-password"}).status_code == 403
        response = c.post("/dashboard/login", headers=HEADERS, json={"password": "test-password"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert all(flag in cookie for flag in ["Secure", "HttpOnly", "SameSite=strict", "Path=/"])
        token = c.cookies.get(COOKIE)
        with session() as s:
            assert s.get(BrowserSession, digest(token))
            assert not s.get(BrowserSession, token)
        assert c.get("/v1/dashboard/bootstrap").status_code == 200
        assert c.post("/v1/processing/pause").status_code == 403
        assert (
            c.post(
                "/v1/processing/pause", headers={**HEADERS, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert c.post("/v1/processing/pause", headers=HEADERS).status_code == 200
        assert c.post("/dashboard/logout", headers=HEADERS).status_code == 200
        assert c.get("/v1/status").status_code == 401
        c.cookies.set(COOKIE, token)
        assert c.get("/v1/status").status_code == 401
        c.cookies.clear()
        c.post("/dashboard/login", headers=HEADERS, json={"password": "test-password"})
        with session() as s, s.begin():
            s.get(BrowserSession, digest(c.cookies.get(COOKIE))).expires_at = now() - timedelta(
                seconds=1
            )
        assert c.get("/dashboard/session").status_code == 401
        c.post("/dashboard/login", headers=HEADERS, json={"password": "test-password"})
        monkeypatch.setenv("MONZO_DASHBOARD_PASSWORD_HASH", hash_password("rotated"))
        settings.cache_clear()
        assert c.get("/dashboard/session").status_code == 401


def test_bootstrap_recorded_years_preserves_gaps_by_account_and_currency(
    client, owner_headers, payment
):
    with session() as s, s.begin():
        for index, (year, currency) in enumerate(
            [(2021, "GBP"), (2021, "GBP"), (2025, "GBP"), (2024, "EUR")]
        ):
            ingest(
                s,
                "acc_test",
                {
                    **payment,
                    "id": f"year_{index}",
                    "created": f"{year}-06-01T12:00:00Z",
                    "currency": currency,
                },
            )
    accounts = client.get("/v1/dashboard/bootstrap", headers=owner_headers).json()["accounts"]
    assert accounts[0]["recorded_years"] == {"GBP": [2021, 2025], "EUR": [2024]}


def test_failed_login_attempts_are_committed_and_rate_limited(monkeypatch):
    configure_login(monkeypatch)
    with TestClient(app, base_url=ORIGIN) as c:
        for _ in range(10):
            assert (
                c.post("/dashboard/login", headers=HEADERS, json={"password": "wrong"}).status_code
                == 401
            )
        response = c.post("/dashboard/login", headers=HEADERS, json={"password": "test-password"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"


def test_api_security_headers_and_owner_boundary(client, automation_headers):
    page = client.get("/")
    assert page.status_code == 404
    for path in ("/assets/app.js", "/assets/app.css", "/assets/tokens.css"):
        assert client.get(path).status_code == 404
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/v1/dashboard/bootstrap", headers=automation_headers).status_code == 403
    assert (
        client.put(
            "/v1/dashboard/transactions/x/approve",
            headers=automation_headers,
            json={"category": "general", "expected_version": 1},
        ).status_code
        == 403
    )


def test_report_reconciles_london_boundaries_currencies_and_accounts(
    client, owner_headers, payment
):
    with session() as s, s.begin():
        s.add(Account(id="acc_other", connection_id=1))
        s.flush()
        for i, amount, created, extra in [
            ("boundary", -100, "2026-09-30T23:00:00Z", {}),  # Oct 1 in London
            ("before", -200, "2026-09-30T22:59:59Z", {}),
            ("dst", -300, "2026-10-25T01:30:00Z", {}),
            ("after", -400, "2026-11-01T00:00:00Z", {}),
            ("credit", 250, "2026-10-03T00:00:00Z", {}),
            ("decline", -500, "2026-10-04T00:00:00Z", {"decline_reason": "OTHER"}),
            ("pending", -600, "2026-10-04T00:00:00Z", {"settled": ""}),
            ("euro", -700, "2026-10-04T00:00:00Z", {"currency": "EUR"}),
        ]:
            ingest(
                s,
                "acc_test",
                {
                    **payment,
                    "id": i,
                    "amount": amount,
                    "created": created,
                    "settled": created,
                    **extra,
                },
            )
        ingest(
            s,
            "acc_other",
            {
                **payment,
                "id": "other",
                "account_id": "acc_other",
                "amount": -999,
                "settled": payment["created"],
            },
        )
        s.get(Transaction, "dst").category = "shopping"
        s.get(Transaction, "dst").category_status = "needs_review"
    response = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers)
    assert response.status_code == 200
    report = response.json()
    assert report["current"]["debits_minor"] == 400
    assert report["current"]["credits_minor"] == 250
    assert report["previous"]["debits_minor"] == 200
    assert sum(report["current"]["series"]) == 400
    assert report["current"]["series"][0] == 100
    assert report["current"]["series"][24] == 300
    assert report["current"]["bucket_categories"][0] == ["uncategorised"]
    assert report["current"]["bucket_categories"][24] == ["shopping"]
    assert report["current"]["bucket_categories"][2] == []  # credits are not spending
    assert report["current"]["bucket_categories"][3] == []  # pending, declined and EUR
    assert len(report["current"]["bucket_categories"]) == 31
    assert sum(g["amount_minor"] for g in report["categories"]) == 400
    assert sum(g["amount_minor"] for g in report["merchants"]) == 400
    assert report["categories"] == [
        {"key": "shopping", "count": 1, "amount_minor": 300, "needs_review_count": 1},
        {"key": "uncategorised", "count": 1, "amount_minor": 100, "needs_review_count": 0},
    ]
    drill = client.get(
        "/v1/dashboard/transactions",
        params={**SCOPE, "category": "shopping", "spending_only": True},
        headers=owner_headers,
    ).json()
    assert [t["id"] for t in drill["items"]] == ["dst"]
    assert drill["items"][0]["category_status"] == "needs_review"
    assert report["review_count"] == 1
    yearly = client.get(
        "/v1/dashboard/report", params={**SCOPE, "mode": "year"}, headers=owner_headers
    ).json()
    assert yearly["current"]["series"][9] == 400
    assert yearly["current"]["bucket_categories"][9] == ["shopping", "uncategorised"]
    assert sum(yearly["current"]["series"]) == 1000
    assert (
        client.get(
            "/v1/dashboard/report", params={**SCOPE, "period": "2026-13"}, headers=owner_headers
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/v1/dashboard/report", params={**SCOPE, "account_id": "missing"}, headers=owner_headers
        ).status_code
        == 404
    )


def test_search_literal_wildcards_and_stable_pagination(client, owner_headers, payment):
    with session() as s, s.begin():
        for i, name in [("a", "100% Store"), ("b", "Other"), ("c", "Third")]:
            ingest(s, "acc_test", {**payment, "id": i, "merchant": {"id": i, "name": name}})
    response = client.get(
        "/v1/dashboard/transactions", params={**SCOPE, "q": "%"}, headers=owner_headers
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == "a"
    first = client.get(
        "/v1/dashboard/transactions", params={**SCOPE, "limit": 2}, headers=owner_headers
    ).json()
    second = client.get(
        "/v1/dashboard/transactions",
        params={**SCOPE, "limit": 2, "offset": 2},
        headers=owner_headers,
    ).json()
    assert [t["id"] for t in first["items"] + second["items"]] == ["a", "b", "c"]


def test_atomic_approval_conflicts_and_future_rule_lifecycle(client, owner_headers, payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
    path = "/v1/dashboard/transactions/tx_test/approve"
    body = {"category": "shopping", "expected_version": 1, "future_rule": True}
    assert (
        client.put(path, headers=owner_headers, json={**body, "expected_version": 2}).status_code
        == 409
    )
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Rule)) == 0
    assert client.put(path, headers=owner_headers, json=body).status_code == 200
    assert client.put(path, headers=owner_headers, json=body).status_code == 409
    detail = client.get("/v1/dashboard/transactions/tx_test", headers=owner_headers).json()
    assert detail["category_source"] == "manual"
    assert detail["rule"] == {"category": "shopping"}
    rules = client.get("/v1/dashboard/rules", headers=owner_headers).json()
    rid = rules[0]["id"]
    assert (
        client.put(
            f"/v1/dashboard/rules/{rid}",
            headers=owner_headers,
            json={"category": "bills", "expected_category": "general"},
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/v1/dashboard/rules/{rid}",
            headers=owner_headers,
            json={"category": "bills", "expected_category": "shopping"},
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/v1/dashboard/rules/{rid}?expected_category=shopping", headers=owner_headers
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/v1/dashboard/rules/{rid}?expected_category=bills", headers=owner_headers
        ).status_code
        == 200
    )
    with session() as s:
        tx = s.get(Transaction, "tx_test")
        assert tx.category == "shopping" and tx.category_source == "manual"
        assert s.scalar(select(func.count()).select_from(Audit)) == 4


def test_approval_rejects_invalid_category_and_missing_merchant_atomically(
    client, owner_headers, payment
):
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "merchant": None})
    path = "/v1/dashboard/transactions/tx_test/approve"
    for category, future in [("made_up", False), ("shopping", True)]:
        assert (
            client.put(
                path,
                headers=owner_headers,
                json={"category": category, "expected_version": 1, "future_rule": future},
            ).status_code
            == 422
        )
    with session() as s:
        assert s.get(Transaction, "tx_test").version == 1
        assert s.scalar(select(func.count()).select_from(Audit)) == 0


def test_empty_report_and_december_rollover(client, owner_headers):
    from monzo_backend.dashboard import bounds

    start, end, previous = bounds("2026-12", "month")
    assert end == datetime(2027, 1, 1, tzinfo=UTC)
    assert previous.month == 11 and start.month == 12
    r = client.get("/v1/dashboard/report", params=SCOPE, headers=owner_headers).json()
    assert r["current"]["debits_minor"] == 0 and r["categories"] == []
    assert len(r["current"]["series"]) == 31


def test_approval_does_not_overwrite_concurrently_changed_merchant_rule(
    client, owner_headers, payment
):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
        s.add(Rule(merchant_id="merch_test", category="bills"))
    path = "/v1/dashboard/transactions/tx_test/approve"
    body = {"category": "shopping", "expected_version": 1, "future_rule": True}
    assert client.put(path, headers=owner_headers, json=body).status_code == 409
    with session() as s:
        assert s.get(Transaction, "tx_test").version == 1
        assert s.scalar(select(Rule)).category == "bills"
    assert (
        client.put(
            path, headers=owner_headers, json={**body, "expected_rule_category": "bills"}
        ).status_code
        == 200
    )


def test_transaction_custom_dates_are_inclusive_london_dates(client, owner_headers, payment):
    with session() as s, s.begin():
        for tx_id, created in [
            ("before-range", "2026-09-30T22:59:59Z"),
            ("start-range", "2026-09-30T23:00:00Z"),
            ("end-range", "2026-10-01T22:59:59Z"),
            ("after-range", "2026-10-01T23:00:00Z"),
        ]:
            ingest(s, "acc_test", {**payment, "id": tx_id, "created": created})
    dates = {"date_from": "2026-10-01", "date_to": "2026-10-01"}
    result = client.get(
        "/v1/dashboard/transactions", params={**SCOPE, **dates}, headers=owner_headers
    )
    assert result.status_code == 200
    assert {t["id"] for t in result.json()["items"]} == {"start-range", "end-range"}
    for invalid in [
        {"date_from": "2026-10-01"},
        {"date_from": "2026-10-02", "date_to": "2026-10-01"},
        {"date_from": "2026-02-30", "date_to": "2026-03-01"},
        {"date_from": "1999-12-31", "date_to": "2026-10-01"},
        {"date_from": "2026-10-01", "date_to": "2100-01-01"},
    ]:
        assert (
            client.get(
                "/v1/dashboard/transactions", params={**SCOPE, **invalid}, headers=owner_headers
            ).status_code
            == 422
        )


def test_all_time_cursor_and_multi_filters(client, owner_headers, payment):
    with session() as s, s.begin():
        for tx_id, created, amount, category in [
            ("cursor-a", "2026-10-12T12:00:00Z", -499, "groceries"),
            ("cursor-b", "2026-10-12T12:00:00Z", 500, "shopping"),
            ("cursor-c", "2025-10-12T12:00:00Z", -2000, "shopping"),
            ("cursor-d", "2024-09-12T12:00:00Z", -10001, "general"),
        ]:
            ingest(s, "acc_test", {**payment, "id": tx_id, "created": created, "amount": amount})
            tx = s.get(Transaction, tx_id)
            tx.category = category
            tx.category_status = "assigned"
    params = {**SCOPE, "all_time": True, "cursor_mode": True, "limit": 2}
    first = client.get("/v1/dashboard/transactions", params=params, headers=owner_headers).json()
    assert [t["id"] for t in first["items"]] == ["cursor-a", "cursor-b"]
    assert first["next_cursor"]
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "id": "newer", "created": "2026-10-13T00:00:00Z"})
    second = client.get(
        "/v1/dashboard/transactions",
        params={**params, "cursor": first["next_cursor"]},
        headers=owner_headers,
    ).json()
    assert [t["id"] for t in second["items"]] == ["cursor-c", "cursor-d"]
    assert second["next_cursor"] is None
    filtered = client.get(
        "/v1/dashboard/transactions",
        params={
            **params,
            "categories": ["groceries", "shopping"],
            "min_amount": 500,
            "max_amount": 2000,
        },
        headers=owner_headers,
    ).json()
    assert [t["id"] for t in filtered["items"]] == ["cursor-b", "cursor-c"]
    for patch in [
        {"cursor": "garbage"},
        {"categories": ["invalid"]},
        {"min_amount": 100, "max_amount": 99},
        {"accounts": ["not-mine"]},
    ]:
        assert client.get(
            "/v1/dashboard/transactions", params={**params, **patch}, headers=owner_headers
        ).status_code in (404, 422)
    one_day = client.get(
        "/v1/dashboard/transactions",
        params={**params, "date_from": "2025-10-12", "date_to": "2025-10-12"},
        headers=owner_headers,
    ).json()
    assert [t["id"] for t in one_day["items"]] == ["cursor-c"]


def test_transaction_dates_stop_at_london_today(client, owner_headers, monkeypatch):
    # UTC is still 16 October; London has crossed midnight into 17 October.
    monkeypatch.setattr(
        "monzo_backend.dashboard.now", lambda: datetime(2026, 10, 16, 23, 30, tzinfo=UTC)
    )
    for day, expected in [("2026-10-17", 200), ("2026-10-18", 422)]:
        response = client.get(
            "/v1/dashboard/transactions",
            params={**SCOPE, "date_from": day, "date_to": day},
            headers=owner_headers,
        )
        assert response.status_code == expected


def test_review_merchants_group_identity_and_separate_conflicts(client, owner_headers, payment):
    with session() as s, s.begin():
        for tx_id, merchant, name, category, source, status in [
            ("a1", "m_a", "Alpha", "groceries", "ai", "needs_review"),
            ("a2", "m_a", "Alpha", "groceries", "ai", "assigned"),
            ("a3", "m_a", "Alpha", "shopping", "manual", "assigned"),
            ("b1", "m_b", "Beta", "groceries", "ai", "needs_review"),
            ("b2", "m_b", "Beta", "shopping", "ai", "needs_review"),
            ("c1", "m_c", "Alpha branch", "shopping", "ai", "needs_review"),
            ("d1", None, "Unknown", None, None, "needs_review"),
            ("d2", None, "Unknown", None, None, "needs_review"),
            ("e1", "m_e", "Declined", "shopping", "ai", "needs_review"),
            ("f1", "m_f", "Card check", "shopping", "ai", "needs_review"),
        ]:
            ingest(
                s, "acc_test", {**payment, "id": tx_id, "merchant": {"id": merchant, "name": name}}
            )
            tx = s.get(Transaction, tx_id)
            tx.category, tx.category_source, tx.category_status = category, source, status
            tx.declined = tx_id == "e1"
            if tx_id == "f1":
                tx.transaction_type = "card_verification"
                tx.amount = 0
    path = "/v1/dashboard/review-merchants"
    page = client.get(path, params={"tab": "ai", "limit": 1}, headers=owner_headers)
    assert page.status_code == 200
    first = page.json()
    assert first["items"][0]["name"] == "Alpha"
    assert first["items"][0]["payment_count"] == 2
    second = client.get(
        path,
        params={"tab": "ai", "limit": 1, "cursor": first["next_cursor"]},
        headers=owner_headers,
    ).json()
    assert second["items"][0]["name"] == "Alpha branch"
    assert second["next_cursor"] is None
    needs = client.get(path, params={"tab": "needs_review"}, headers=owner_headers).json()["items"]
    assert [m["name"] for m in needs] == ["Beta", "Unknown", "Unknown"]
    assert all(m["category"] is None for m in needs)
    assert client.get(path, params={"cursor": "bad"}, headers=owner_headers).status_code == 422
    search = client.get(
        path, params={"tab": "needs_review", "q": "Beta"}, headers=owner_headers
    ).json()
    assert len(search["items"]) == 1


def test_review_chains_combine_branches_but_not_similar_names(client, owner_headers, payment):
    with session() as s, s.begin():
        for tid, mid, name in [
            ("aldi_one", "aldi1", "Aldi"),
            ("aldi_two", "aldi2", " ALDI "),
            ("similar", "other", "Aldi Local Services"),
            ("no_id", None, "Aldi"),
        ]:
            tx = ingest(
                s, "acc_test", {**payment, "id": tid, "merchant": {"id": mid, "name": name}}
            )
            tx.category, tx.category_source, tx.category_status = "groceries", "ai", "needs_review"
    response = client.get("/v1/dashboard/review-merchants?tab=ai", headers=owner_headers)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    chain = next(row for row in rows if row["name"] == "Aldi" and row["payment_count"] == 2)
    assert chain["name"] == "Aldi"
    assert chain["payment_count"] == 2
    assert len(rows) == 3
    with session() as s, s.begin():
        s.get(Transaction, "aldi_two").category = "shopping"
    response = client.get("/v1/dashboard/review-merchants?tab=needs_review", headers=owner_headers)
    chain = next(
        row
        for row in response.json()["items"]
        if row["name"] == "Aldi" and row["payment_count"] == 2
    )
    assert chain["category"] is None
    assert chain["payment_count"] == 2
