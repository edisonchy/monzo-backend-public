from datetime import timedelta

from sqlalchemy import select

from monzo_backend.db import session
from monzo_backend.domain import deterministic, ingest
from monzo_backend.models import Account, Audit, Rule, Transaction

BASE = "/v1/dashboard/transactions/tx_test/"


def seed(payment):
    with session() as s, s.begin():
        s.add(Account(id="flex", connection_id=1, kind="uk_monzo_flex"))
        s.add(Account(id="rewards", connection_id=1, kind="uk_rewards"))
        s.flush()
        for tid, source, status, account in [
            ("tx_test", "ai", "needs_review", "acc_test"),
            ("unresolved", "review", "needs_review", "flex"),
            ("manual", "manual", "assigned", "acc_test"),
            ("bank", "monzo", "assigned", "acc_test"),
            ("rule", "rule", "assigned", "acc_test"),
            ("ai_accepted", "ai", "assigned", "acc_test"),
            ("reward", "ai", "needs_review", "rewards"),
            ("declined", "review", "needs_review", "acc_test"),
            ("same_name_other_id", "ai", "needs_review", "acc_test"),
        ]:
            data = {**payment, "id": tid, "account_id": account}
            if tid == "declined":
                data["decline_reason"] = "OTHER"
            if tid == "same_name_other_id":
                data["merchant"] = {"id": "different", "name": payment["merchant"]["name"]}
            tx = ingest(s, account, data)
            tx.category = None if tid == "unresolved" else "shopping"
            tx.category_source, tx.category_status = source, status


def choice(**extra):
    return {
        "category": "groceries",
        "scope": "unverified",
        "future_rule": False,
        "expected_version": 1,
        **extra,
    }


def preview(client, headers, body, path=BASE):
    r = client.post(path + "category-preview", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def apply(client, headers, body, p, **extra):
    return client.put(
        BASE + "category-change",
        headers=headers,
        json={**body, "preview_token": p["preview_token"], **extra},
    )


def test_unverified_scope_matches_exact_identity_and_preserves_verified(
    client, owner_headers, payment
):
    seed(payment)
    body = choice()
    p = preview(client, owner_headers, body)
    assert (p["matched_count"], p["change_count"], p["protected_count"], p["declined_count"]) == (
        6,
        3,
        3,
        1,
    )
    r = apply(client, owner_headers, body, p)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 3
    with session() as s:
        for tid in ["tx_test", "unresolved", "ai_accepted"]:
            t = s.get(Transaction, tid)
            assert (t.category, t.category_source, t.category_status, t.version) == (
                "groceries",
                "manual",
                "assigned",
                2,
            )
        for tid in ["manual", "bank", "rule", "reward", "declined", "same_name_other_id"]:
            assert s.get(Transaction, tid).category == "shopping"
        assert s.scalar(select(Rule)) is None
    assert apply(client, owner_headers, body, p).status_code == 409


def test_all_scope_requires_explicit_verified_confirmation_and_undo(client, owner_headers, payment):
    seed(payment)
    body = choice(scope="all", future_rule=True)
    p = preview(client, owner_headers, body)
    assert p["verified_replaced_count"] == 3 and p["change_count"] == 6
    assert apply(client, owner_headers, body, p).status_code == 422
    with session() as s:
        assert s.get(Transaction, "tx_test").version == 1
        assert s.scalar(select(Rule)) is None
    r = apply(client, owner_headers, body, p, confirm_verified=True)
    assert r.status_code == 200, r.text
    batch = r.json()["batch_id"]
    with session() as s:
        assert s.scalar(select(Rule)).effective_from is not None
    undo_path = "/v1/dashboard/category-changes/" + batch + "/undo"
    result = client.post(undo_path, headers=owner_headers)
    assert result.status_code == 200, result.text
    assert result.json()["restored"] == 6
    with session() as s:
        assert s.get(Transaction, "unresolved").category is None
        assert s.get(Transaction, "manual").category == "shopping"
        assert s.get(Transaction, "manual").version == 3
        assert s.get(Transaction, "tx_test").category_source == "ai"
        assert s.scalar(select(Rule)) is None
    assert client.post(undo_path, headers=owner_headers).status_code == 409


def test_preview_detects_changed_target_new_match_rule_and_tampering(
    client, owner_headers, payment
):
    seed(payment)
    body = choice()
    p = preview(client, owner_headers, body)
    assert apply(client, owner_headers, {**body, "category": "bills"}, p).status_code == 409
    with session() as s, s.begin():
        s.get(Transaction, "unresolved").version += 1
    assert apply(client, owner_headers, body, p).status_code == 409
    p = preview(client, owner_headers, body)
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "id": "new_match"})
    assert apply(client, owner_headers, body, p).status_code == 409
    p = preview(client, owner_headers, body)
    with session() as s, s.begin():
        s.add(Rule(merchant_id="merch_test", category="health"))
    assert apply(client, owner_headers, body, p).status_code == 409
    with session() as s:
        assert s.get(Transaction, "tx_test").category == "shopping"
        assert not s.scalar(select(Audit).where(Audit.action == "category_batch_applied"))


def test_this_only_future_cutoff_and_existing_rule_off(client, owner_headers, payment):
    seed(payment)
    body = choice(scope="this", future_rule=True)
    p = preview(client, owner_headers, body)
    assert p["change_count"] == 1
    r = apply(client, owner_headers, body, p)
    assert r.status_code == 200, r.text
    with session() as s, s.begin():
        rule = s.scalar(select(Rule))
        old = s.get(Transaction, "unresolved")
        old.created_at = rule.effective_from - timedelta(seconds=1)
        assert deterministic(s, old) is None
        old.created_at = rule.effective_from
        assert deterministic(s, old)["category"] == "groceries"
        assert s.get(Transaction, "manual").category == "shopping"
    body = choice(scope="this", category="bills", expected_version=2)
    p = preview(client, owner_headers, body)
    assert p["existing_rule"]["category"] == "groceries"
    assert apply(client, owner_headers, body, p).status_code == 200
    with session() as s:
        assert s.scalar(select(Rule)).category == "groceries"


def test_missing_merchant_declined_rewards_and_auth_boundaries(
    client, owner_headers, automation_headers, payment
):
    seed(payment)
    with session() as s, s.begin():
        s.get(Transaction, "tx_test").merchant_id = None
    for body in [choice(), choice(scope="this", future_rule=True)]:
        assert (
            client.post(BASE + "category-preview", headers=owner_headers, json=body).status_code
            == 422
        )
    body = choice(scope="this")
    p = preview(client, owner_headers, body)
    assert apply(client, owner_headers, body, p).status_code == 200
    for tid, status in [("declined", 409), ("reward", 404)]:
        assert (
            client.post(
                f"/v1/dashboard/transactions/{tid}/category-preview",
                headers=owner_headers,
                json=body,
            ).status_code
            == status
        )
    assert (
        client.post(BASE + "category-preview", headers=automation_headers, json=body).status_code
        == 403
    )
    assert (
        client.put(
            BASE + "category-change",
            headers=automation_headers,
            json={**body, "preview_token": "bad"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/dashboard/category-changes/missing/undo", headers=automation_headers
        ).status_code
        == 403
    )


def test_undo_refuses_newer_payment_or_rule_and_is_atomic(client, owner_headers, payment):
    seed(payment)
    body = choice(future_rule=True)
    r = apply(client, owner_headers, body, preview(client, owner_headers, body))
    batch = r.json()["batch_id"]
    with session() as s, s.begin():
        s.get(Transaction, "unresolved").version += 1
    assert (
        client.post(
            "/v1/dashboard/category-changes/" + batch + "/undo", headers=owner_headers
        ).status_code
        == 409
    )
    with session() as s:
        assert s.get(Transaction, "tx_test").category == "groceries"
        assert s.scalar(select(Rule)).category == "groceries"


def test_expired_preview_and_verified_selected_protection(
    client, owner_headers, payment, monkeypatch
):
    seed(payment)
    body = choice()
    with session() as s, s.begin():
        t = s.get(Transaction, "tx_test")
        t.category_source = "manual"
        t.category_status = "assigned"
    monkeypatch.setattr("monzo_backend.category_changes.time.time", lambda: 10000)
    p = preview(client, owner_headers, body)
    assert p["protected_count"] == 4 and p["change_count"] == 2
    monkeypatch.setattr("monzo_backend.category_changes.time.time", lambda: 10901)
    assert apply(client, owner_headers, body, p).status_code == 409


def test_rule_only_change_and_undo_restore_existing_rule(client, owner_headers, payment):
    seed(payment)
    with session() as s, s.begin():
        tx = s.get(Transaction, "tx_test")
        tx.category, tx.category_source, tx.category_status = "groceries", "manual", "assigned"
        s.add(Rule(merchant_id="merch_test", category="health"))
    body = choice(scope="this", future_rule=True)
    p = preview(client, owner_headers, body)
    assert p["change_count"] == 0
    r = apply(client, owner_headers, body, p)
    assert r.status_code == 200, r.text
    undo_path = "/v1/dashboard/category-changes/" + r.json()["batch_id"] + "/undo"
    assert client.post(undo_path, headers=owner_headers).status_code == 200
    with session() as s:
        rule = s.scalar(select(Rule))
        assert rule.category == "health" and rule.effective_from is None
        assert s.get(Transaction, "tx_test").version == 1
    assert client.post(undo_path, headers=owner_headers).status_code == 409


def test_undo_refuses_changed_rule_without_restoring_any_payment(client, owner_headers, payment):
    seed(payment)
    body = choice(future_rule=True)
    result = apply(client, owner_headers, body, preview(client, owner_headers, body)).json()
    with session() as s, s.begin():
        s.scalar(select(Rule)).category = "bills"
    assert (
        client.post(
            "/v1/dashboard/category-changes/" + result["batch_id"] + "/undo", headers=owner_headers
        ).status_code
        == 409
    )
    with session() as s:
        assert s.get(Transaction, "tx_test").category == "groceries"
        assert s.scalar(select(Rule)).category == "bills"


def test_chain_review_changes_all_branches_preserves_verified_and_undoes_rules(
    client, owner_headers, payment
):
    seed(payment)
    with session() as s, s.begin():
        for tx in s.scalars(select(Transaction)).all():
            tx.merchant_name = "Aldi"
    body = choice(group_merchant=True, future_rule=True)
    p = preview(client, owner_headers, body)
    assert p["change_count"] == 4
    assert p["protected_count"] == 3
    r = apply(client, owner_headers, body, p)
    assert r.status_code == 200, r.text
    with session() as s:
        assert s.get(Transaction, "same_name_other_id").category == "groceries"
        assert s.get(Transaction, "manual").category == "shopping"
        assert s.get(Transaction, "bank").category == "shopping"
        assert s.get(Transaction, "rule").category == "shopping"
        assert s.get(Transaction, "reward").category == "shopping"
        assert len(s.scalars(select(Rule)).all()) == 1
    response = client.post(
        "/v1/dashboard/category-changes/" + r.json()["batch_id"] + "/undo",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    with session() as s:
        assert s.get(Transaction, "same_name_other_id").category == "shopping"
        assert not s.scalars(select(Rule)).all()
    bad = client.post(
        BASE + "category-preview",
        headers=owner_headers,
        json=choice(group_merchant=True, scope="all"),
    )
    assert bad.status_code == 422


def test_chain_preview_rejects_changed_membership(client, owner_headers, payment):
    seed(payment)
    with session() as s, s.begin():
        s.get(Transaction, "tx_test").merchant_name = "Aldi"
    body = choice(group_merchant=True)
    p = preview(client, owner_headers, body)
    with session() as s, s.begin():
        ingest(
            s,
            "acc_test",
            {
                **payment,
                "id": "new_branch",
                "merchant": {"id": "new_branch_id", "name": "Test Shop"},
            },
        )
    assert apply(client, owner_headers, body, p).status_code == 409


def test_matching_new_names_group_without_a_curated_list(client, owner_headers, payment):
    seed({**payment, "merchant": {"id": "merch_test", "name": "Independent Cafe"}})
    with session() as s, s.begin():
        for tx in s.scalars(select(Transaction)).all():
            tx.merchant_name = "Independent Cafe"
    body = choice(group_merchant=True)
    p = preview(client, owner_headers, body)
    assert p["change_count"] == 4
    assert apply(client, owner_headers, body, p).status_code == 200
    with session() as s:
        assert s.get(Transaction, "same_name_other_id").category == "groceries"


def test_custom_category_persists_and_reports_settled_spending(client, owner_headers, payment):
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", {**payment, "settled": payment["created"]})
        tx.category, tx.category_source, tx.category_status = "groceries", "manual", "assigned"
    url = "/v1/dashboard/categories"
    data = {"name": " Coffee & Tea ", "colour": "#AB457D"}
    created = client.post(url, json=data, headers=owner_headers)
    assert created.status_code == 200
    key = created.json()["key"]
    assert client.post(url, json=data, headers=owner_headers).json() == created.json()
    assert (
        client.post(url, json={**data, "colour": "#123456"}, headers=owner_headers).status_code
        == 409
    )
    assert (
        client.post(url, json={**data, "colour": "bad"}, headers=owner_headers).status_code == 422
    )
    bootstrap = client.get("/v1/dashboard/bootstrap", headers=owner_headers).json()
    assert bootstrap["categories"].count(key) == 1
    assert bootstrap["category_colours"][key] == "#ab457d"
    body = choice(category=key, scope="this")
    result = apply(client, owner_headers, body, preview(client, owner_headers, body))
    assert result.status_code == 200
    report = client.get(
        "/v1/dashboard/report",
        params={"account_id": "combined", "period": "2026-10", "category": key},
        headers=owner_headers,
    ).json()
    assert report["categories"][0]["key"] == key
    assert report["current"]["bucket_categories"][14] == [key]


def test_remove_category_checks_usage_and_moves_payments_and_rules(client, owner_headers, payment):
    url = "/v1/dashboard/categories"
    key = client.post(
        url, json={"name": "Bakery", "colour": "#ffa055"}, headers=owner_headers
    ).json()["key"]

    def usage():
        return client.get(url, params={"category": key}, headers=owner_headers).json()

    def remove(revision, replacement=None):
        return client.request(
            "DELETE",
            url,
            headers=owner_headers,
            json={
                "category": key,
                "revision": revision,
                "replacement": replacement,
            },
        )

    empty = usage()
    with session() as s, s.begin():
        tx = ingest(s, "acc_test", payment)
        tx.category, tx.category_status = key, "assigned"
        s.add(Rule(category=key, merchant_id="baker"))
    assert remove(empty["revision"]).status_code == 409
    used = usage()
    assert (used["payments"], used["rules"]) == (1, 1)
    assert remove(used["revision"]).status_code == 409
    assert remove(used["revision"], key).status_code == 422
    assert remove(used["revision"], "custom:missing").status_code == 422
    assert remove(used["revision"], "groceries").status_code == 200
    assert remove(used["revision"], "groceries").status_code == 404
    with session() as s:
        assert s.get(Transaction, payment["id"]).category == "groceries"
        assert s.scalar(select(Rule.category)) == "groceries"
    assert (
        key not in client.get("/v1/dashboard/bootstrap", headers=owner_headers).json()["categories"]
    )
    client.post(url, json={"name": "Bakery", "colour": "#ffaa55"}, headers=owner_headers)
    assert remove(empty["revision"]).status_code == 409
    assert remove(usage()["revision"]).status_code == 200


def test_category_deletion_preserves_budget_history(client, owner_headers):
    from monzo_backend import budgets
    from monzo_backend.models import now

    url = "/v1/dashboard/categories"
    category = client.post(
        url, json={"name": "Lunch", "colour": "#ffcc00"}, headers=owner_headers
    ).json()["key"]
    period = now().astimezone(budgets.TZ).strftime("%Y-%m")
    edit = budgets.BudgetEdit(
        category=category, period=period, amount_minor=1500, expected_version=0
    )
    budgets.save(edit)

    def delete():
        usage = client.get(url, params={"category": category}, headers=owner_headers).json()
        return client.request(
            "DELETE",
            url,
            json={"category": category, "revision": usage["revision"]},
            headers=owner_headers,
        )

    assert delete().status_code == 409
    budgets.save(edit.model_copy(update={"amount_minor": None, "expected_version": 1}))
    assert delete().status_code == 200
    item = next(i for i in budgets.report(period)["items"] if i["category"] == category)
    assert [v["amount_minor"] for v in item["history"]] == [None, 1500]
