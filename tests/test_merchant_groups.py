from datetime import timedelta

from sqlalchemy import select

from monzo_backend import merchant_groups
from monzo_backend.db import session
from monzo_backend.domain import deterministic, ingest
from monzo_backend.models import MerchantGroup, MerchantIdentity, Rule, Transaction, now


def add(s, payment, tid, mid, name):
    return ingest(s, "acc_test", {**payment, "id": tid, "merchant": {"id": mid, "name": name}})


def test_new_identity_uses_group_rule_and_protects_verified(payment):
    with session() as s, s.begin():
        first = add(s, payment, "one", "branch1", "New Cafe")
        group = merchant_groups.for_merchant(s, first.merchant_id)
        s.add(
            Rule(group_id=group.id, category="eating_out", effective_from=now() - timedelta(days=1))
        )
        newer = add(s, {**payment, "created": now().isoformat()}, "two", "branch2", "  NEW   CAFE ")
        assert merchant_groups.for_merchant(s, newer.merchant_id).id == group.id
        assert deterministic(s, newer)["category"] == "eating_out"
        newer.category, newer.category_source, newer.category_status = "health", "monzo", "assigned"
        assert deterministic(s, newer)["category"] == "health"
        unnamed = add(s, payment, "three", None, "New Cafe")
        assert merchant_groups.for_merchant(s, unnamed.merchant_id) is None


def test_merge_remembers_aliases_and_rule_conflicts(client, owner_headers, payment):
    with session() as s, s.begin():
        first = add(s, payment, "one", "a", "Coffee Shop")
        second = add(s, payment, "two", "b", "Coffee Shop Lancaster")
        first.category, first.category_source, first.category_status = (
            "health",
            "manual",
            "assigned",
        )
        target = merchant_groups.for_merchant(s, first.merchant_id)
        source = merchant_groups.for_merchant(s, second.merchant_id)
        s.add(Rule(group_id=target.id, category="eating_out"))
        s.add(Rule(group_id=source.id, category="groceries"))
        body = {
            "target_id": target.id,
            "source_id": source.id,
            "target_version": target.version,
            "source_version": source.version,
        }
    assert (
        client.post(
            "/v1/dashboard/merchant-groups/merge", headers=owner_headers, json=body
        ).status_code
        == 422
    )
    response = client.post(
        "/v1/dashboard/merchant-groups/merge",
        headers=owner_headers,
        json={**body, "rule_policy": "target"},
    )
    assert response.status_code == 200, response.text
    assert (
        client.post(
            "/v1/dashboard/merchant-groups/merge", headers=owner_headers, json=body
        ).status_code
        == 409
    )
    with session() as s, s.begin():
        tx = add(
            s, {**payment, "created": now().isoformat()}, "third", "c", "Coffee Shop Lancaster"
        )
        assert s.get(MerchantIdentity, "c").group_id == body["target_id"]
        assert deterministic(s, tx)["category"] == "eating_out"
        assert s.get(Transaction, "one").category == "health"
        assert s.get(Transaction, "two").category is None
        assert s.get(MerchantGroup, body["source_id"]).merged_into == body["target_id"]
        assert len(s.scalars(select(Rule)).all()) == 1


def test_new_identity_invalidates_merge_version(client, owner_headers, payment):
    with session() as s, s.begin():
        add(s, payment, "a", "a", "One")
        add(s, payment, "b", "b", "Two")
        a = merchant_groups.for_merchant(s, "a")
        b = merchant_groups.for_merchant(s, "b")
        body = {
            "target_id": a.id,
            "source_id": b.id,
            "target_version": a.version,
            "source_version": b.version,
        }
        add(s, payment, "c", "c", "One")
    assert (
        client.post(
            "/v1/dashboard/merchant-groups/merge", headers=owner_headers, json=body
        ).status_code
        == 409
    )
