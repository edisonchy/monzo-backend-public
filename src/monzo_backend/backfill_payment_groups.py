"""Register explicit owner-approved business groups and attach an exact payment manifest."""

import argparse
import json
import sys
from collections import Counter

from sqlalchemy import select

from monzo_backend import merchant_groups
from monzo_backend.backfill_payment_identity import fetch_identity
from monzo_backend.db import session
from monzo_backend.domain import enqueue
from monzo_backend.models import (
    Audit,
    MerchantGroup,
    MerchantIdentity,
    PaymentGroupMatcher,
    Transaction,
)
from monzo_backend.payment_groups import PREFIX, confirmed_matcher
from monzo_backend.payment_identity import description_label, payment_name


def backfill(manifest, *, apply=False, fetch=fetch_identity):
    rows = manifest["payments"]
    ids = [r["id"] for r in rows]
    if not 0 < len(rows) <= 200 or len(set(ids)) != len(ids):
        raise ValueError("Expected 1–200 explicit distinct payments")
    with session() as s:
        previous = s.scalar(
            select(Audit).where(
                Audit.action == "payment_groups_backfilled",
                Audit.detail["batch_id"].astext == manifest["batch_id"],
            )
        )
        if previous:
            if previous.detail["payments"] != rows:
                raise ValueError("Batch identity reused")
            return {"already_applied": True, "selected": len(rows)}
    payloads = {tid: fetch(tid) for tid in ids}
    counts, groups, matchers = Counter(), {}, set()
    with session() as s, s.begin():
        merchant_groups.lock(s)
        for row in sorted(rows, key=lambda r: r["id"]):
            tx = s.scalar(select(Transaction).where(Transaction.id == row["id"]).with_for_update())
            data = payloads[row["id"]]
            if (
                not tx
                or tx.merchant_id
                or tx.payment_group_identity
                or tx.declined
                or tx.version != row["expected_version"]
                or payment_name(tx) != row["name"]
                or data.get("id") != tx.id
                or data.get("account_id") != tx.account_id
                or data.get("amount") != tx.amount
                or data.get("currency") != tx.currency
                or tx.amount != row["amount"]
                or tx.currency != row["currency"]
            ):
                raise ValueError("Payment changed; inspect a fresh manifest")
            facts = confirmed_matcher(data)
            # Fresh bank names must still support the exact owner-confirmed label.
            if facts.get("name") and facts["name"] != merchant_groups.normalise(row["name"]):
                raise ValueError("Counterparty changed")
            if (
                not facts.get("name")
                and description_label(data.get("description"), data.get("scheme"))[0] != row["name"]
            ):
                raise ValueError("Subscription description changed")
            merchant = data.get("merchant")
            if merchant and (not isinstance(merchant, dict) or merchant.get("id")):
                raise ValueError("Bank merchant identity changed")
            label = row["group"]
            if not isinstance(label, str) or not label.strip() or len(label) > 100:
                raise ValueError("Invalid group name")
            counts[label + ":" + facts["direction"]] += 1
            if not apply:
                continue
            if label not in groups:
                if s.scalar(select(MerchantGroup.id).where(MerchantGroup.name == label)):
                    raise ValueError("Group already exists; review instead of duplicating it")
                group = MerchantGroup(name=label)
                s.add(group)
                s.flush()
                groups[label] = group
            group = groups[label]
            identity_id = PREFIX + group.id + "_" + facts["direction"]
            if not s.get(MerchantIdentity, identity_id):
                s.add(MerchantIdentity(merchant_id=identity_id, group_id=group.id))
                s.flush()
            key = (tx.account_id, identity_id, json.dumps(facts, sort_keys=True))
            if key not in matchers:
                matchers.add(key)
                s.add(
                    PaymentGroupMatcher(
                        account_id=tx.account_id, identity_id=identity_id, evidence=facts
                    )
                )
            tx.payment_group_identity = identity_id
            tx.version += 1  # Fence existing previews and workers; preserve category decisions.
            if tx.category_status == "pending" and tx.category_source != "manual":
                enqueue(
                    s,
                    "category",
                    "classify",
                    f"category:{tx.id}:{tx.version}:v1",
                    {"transaction_id": tx.id, "version": tx.version},
                )
        result = {"selected": len(rows), "applied": apply, "groups": dict(counts)}
        if apply:
            s.add(
                Audit(
                    actor="owner",
                    action="payment_groups_backfilled",
                    detail={
                        **result,
                        "batch_id": manifest["batch_id"],
                        "payments": rows,
                        "group_ids": {name: group.id for name, group in groups.items()},
                        "matcher_count": len(matchers),
                    },
                )
            )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = json.load(sys.stdin if args.manifest == "-" else open(args.manifest))
    print(json.dumps(backfill(manifest, apply=args.apply)))
