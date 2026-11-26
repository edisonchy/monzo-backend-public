"""Previewed, atomic owner category changes with exact merchant matching and guarded undo."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import load_only

from monzo_backend import custom_categories, merchant_groups
from monzo_backend.card_checks import is_card_check
from monzo_backend.config import settings
from monzo_backend.domain import CATEGORIES
from monzo_backend.models import Account, Audit, MerchantIdentity, Rule, Transaction, now, uid
from monzo_backend.spending import ACCOUNT_KINDS, eligible_accounts

# These are the payment facts used by both dashboard and private command previews.
PAYMENT_COLUMNS = load_only(
    Transaction.id,
    Transaction.account_id,
    Transaction.merchant_id,
    Transaction.payment_group_identity,
    Transaction.amount,
    Transaction.currency,
    Transaction.created_at,
    Transaction.settled_at,
    Transaction.declined,
    Transaction.transaction_type,
    Transaction.version,
    Transaction.category,
    Transaction.category_source,
    Transaction.category_status,
)
ACCOUNT_COLUMNS = load_only(Account.id, Account.kind)


class CategoryChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(max_length=100)
    scope: Literal["this", "unverified", "all"] = "this"
    future_rule: bool = False
    group_merchant: bool = False
    expected_version: int = Field(ge=1)


class CategoryApply(CategoryChoice):
    preview_token: str = Field(max_length=100)
    confirm_verified: bool = False


@dataclass(frozen=True)
class CategoryActor:
    """Trusted identity supplied by the command service, never by a tool payload."""

    issuer: str
    subject: str
    client: str

    def __post_init__(self):
        for value in (self.issuer, self.subject, self.client):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError("Invalid category actor")

    def attribution(self):
        return {
            "issuer": self.issuer,
            "subject": self.subject,
            "client": self.client,
            "origin": "mcp",
        }


def attributed_detail(detail, actor):
    return {**detail, "attribution": actor.attribution()} if actor else detail


def verified(tx):
    return (
        tx.category_status == "assigned"
        and (tx.category in CATEGORIES or (tx.category or "").startswith("custom:"))
        and tx.category_source in ("manual", "rule", "monzo")
    )


def category_state(tx):
    return {"category": tx.category, "source": tx.category_source, "status": tx.category_status}


def rule_state(rule):
    return (
        {
            "id": rule.id,
            "category": rule.category,
            "effective_from": rule.effective_from.isoformat() if rule.effective_from else None,
        }
        if rule
        else None
    )


def merchant_lock(s, merchant_id):
    if merchant_id:
        s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:merchant, 0))"),
            {"merchant": merchant_id},
        )


def snapshot(s, tx_id, choice, lock=False, *, frozen_ids=None, max_rows=None):
    if not custom_categories.known(s, choice.category):
        raise HTTPException(422, "Unknown category")
    if lock:
        merchant_groups.lock(s)
    tx = s.get(Transaction, tx_id, options=[PAYMENT_COLUMNS])
    if not tx or s.get(Account, tx.account_id, options=[ACCOUNT_COLUMNS]).kind not in ACCOUNT_KINDS:
        raise HTTPException(404, "Payment not found")
    merchant = tx.category_identity
    if (choice.scope != "this" or choice.future_rule) and not merchant:
        raise HTTPException(422, "An exact merchant ID is required for merchant-wide changes")
    if choice.group_merchant and choice.scope != "unverified":
        raise HTTPException(422, "Group changes must preserve verified categories")
    group = merchant_groups.for_merchant(s, merchant)
    directional = bool(tx.payment_group_identity)
    merchants = [merchant] if merchant else []
    if group and choice.group_merchant and not directional:
        members = select(MerchantIdentity.merchant_id).where(MerchantIdentity.group_id == group.id)
        merchants = sorted(s.scalars(members.limit(max_rows + 1) if max_rows else members).all())
        if max_rows and len(merchants) > max_rows:
            raise HTTPException(422, "batch_too_large")
    future_targets = ["group:" + group.id] if group and not directional else merchants
    rule_targets = future_targets + [m for m in merchants if m not in future_targets]
    if lock:
        for merchant_id in merchants:
            merchant_lock(s, merchant_id)
    query = (
        select(Transaction)
        .options(PAYMENT_COLUMNS)
        .where(
            Transaction.account_id.in_(eligible_accounts()),
            Transaction.id == tx_id
            if choice.scope == "this"
            else Transaction.category_identity.in_(merchants),
        )
        .order_by(Transaction.id)
    )
    if frozen_ids is not None:
        query = query.where(Transaction.id.in_(frozen_ids))
    if max_rows:
        query = query.limit(max_rows + 1)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    rows = s.scalars(query).all()
    if max_rows and len(rows) > max_rows:
        raise HTTPException(422, "batch_too_large")
    selected = next((t for t in rows if t.id == tx_id), None)
    if (
        not selected
        or selected.category_identity != merchant
        or selected.version != choice.expected_version
    ):
        raise HTTPException(409, "Payment changed; reopen the category picker")
    if is_card_check(selected):
        raise HTTPException(409, "Card verifications do not need a spending category")
    if selected.declined:
        raise HTTPException(409, "Declined payments cannot be approved")
    rule_query = (
        select(Rule)
        .where(
            (Rule.group_id == group.id) | Rule.merchant_id.in_(merchants)
            if group and not directional
            else Rule.merchant_id.in_(merchants)
        )
        .order_by(Rule.id)
    )
    if lock:
        rule_query = rule_query.with_for_update().execution_options(populate_existing=True)
    rules = {
        ("group:" + r.group_id if r.group_id else r.merchant_id): r
        for r in s.scalars(rule_query).all()
    }
    rule = (rules.get("group:" + group.id) if group and not directional else None) or rules.get(
        merchant
    )
    candidates = [t for t in rows if not t.declined and not is_card_check(t)]
    protected = [t for t in candidates if choice.scope == "unverified" and verified(t)]
    targets = [
        t
        for t in candidates
        if t not in protected
        and category_state(t)
        != {"category": choice.category, "source": "manual", "status": "assigned"}
    ]
    replaced = sum(verified(t) and t.category != choice.category for t in targets)
    payload = {
        "tx_id": tx_id,
        "choice": CategoryChoice(
            **choice.model_dump(include=set(CategoryChoice.model_fields))
        ).model_dump(),
        "rules": {mid: rule_state(rules.get(mid)) for mid in rule_targets},
        "future_targets": future_targets,
        "group": {"id": group.id, "version": group.version} if group else None,
        "rows": [
            [
                t.id,
                t.account_id,
                t.category_identity,
                t.version,
                category_state(t),
                t.amount,
                t.currency,
                t.created_at.isoformat(),
                bool(t.declined),
                t.transaction_type,
                t.settled_at.isoformat() if t.settled_at else None,
            ]
            for t in rows
        ],
    }
    counts = {
        "matched_count": len(candidates),
        "change_count": len(targets),
        "protected_count": len(protected),
        "verified_replaced_count": replaced,
        "unchanged_count": len(candidates) - len(targets) - len(protected),
        "declined_count": sum(bool(t.declined) for t in rows),
        "card_check_count": sum(is_card_check(t) for t in rows),
        "existing_rule": rule_state(rule),
    }
    return selected, rules, targets, payload, counts


def token(payload, issued):
    content = json.dumps([issued, payload], sort_keys=True, separators=(",", ":"))
    key = settings().owner_token.get_secret_value().encode()
    return str(issued) + "." + hmac.new(key, content.encode(), hashlib.sha256).hexdigest()


def preview(s, tx_id, choice):
    *_, payload, counts = snapshot(s, tx_id, choice)
    return {**counts, "preview_token": token(payload, int(time.time()))}


def apply(s, tx_id, choice):
    tx, rules, targets, payload, counts = snapshot(s, tx_id, choice, lock=True)
    try:
        issued = int(choice.preview_token.split(".")[0])
    except ValueError:
        raise HTTPException(409, "Preview expired; check the updated counts") from None
    if not 0 <= time.time() - issued <= 900 or not hmac.compare_digest(
        choice.preview_token, token(payload, issued)
    ):
        raise HTTPException(409, "Payments or rules changed; check a fresh preview before applying")
    if choice.scope == "all" and counts["verified_replaced_count"] and not choice.confirm_verified:
        raise HTTPException(422, "Confirm replacement of verified categories first")
    if not targets and not choice.future_rule:
        raise HTTPException(422, "No payments need this change")
    return apply_snapshot(s, tx, rules, targets, payload, counts)


def apply_snapshot(s, tx, rules, targets, payload, counts, actor=None):
    """Mutate an already locked/validated snapshot; caller owns the transaction."""
    choice = CategoryChoice.model_validate(payload["choice"])
    tx_id = payload["tx_id"]
    before_rules = {mid: payload["rules"][mid] for mid in payload["future_targets"]}
    changes = []
    for row in targets:
        before = category_state(row)
        row.category, row.category_source, row.category_status = (
            choice.category,
            "manual",
            "assigned",
        )
        row.version += 1
        row.updated_at = now()
        changes.append({"id": row.id, "before": before, "after_version": row.version})
    if choice.future_rule:
        if payload["group"]:
            s.get(merchant_groups.MerchantGroup, payload["group"]["id"]).version += 1
        for mid in before_rules:
            rule = rules.get(mid)
            if not rule:
                rule = Rule(
                    id=uid(),
                    category=choice.category,
                    **({"group_id": mid[6:]} if mid.startswith("group:") else {"merchant_id": mid}),
                )
                s.add(rule)
                rules[mid] = rule
            rule.category, rule.effective_from = choice.category, now()
        s.flush()  # Rule IDs/defaults must exist in the durable after-state.
    batch_id = uid()
    s.add(
        Audit(
            id=batch_id,
            actor="mcp" if actor else "owner",
            action="category_batch_applied",
            detail=attributed_detail(
                {
                    "transaction_id": tx_id,
                    "merchant_id": tx.category_identity,
                    "category": choice.category,
                    "scope": choice.scope,
                    "future_rule": choice.future_rule,
                    "changes": changes,
                    "rules_before": before_rules,
                    "rules_after": {mid: rule_state(rules.get(mid)) for mid in before_rules},
                    "verified_replaced": counts["verified_replaced_count"],
                    **(
                        {
                            "group_after": {
                                "id": payload["group"]["id"],
                                "version": s.get(
                                    merchant_groups.MerchantGroup, payload["group"]["id"]
                                ).version,
                            }
                        }
                        if actor
                        and payload["group"]
                        and (choice.scope != "this" or choice.future_rule)
                        else {}
                    ),
                },
                actor,
            ),
        )
    )
    return {"batch_id": batch_id, "changed": len(changes), "future_rule": choice.future_rule}


def undo_snapshot(s, batch_id, actor=None):
    merchant_groups.lock(s)
    batch = s.get(Audit, batch_id)
    if not batch or batch.action != "category_batch_applied":
        raise HTTPException(404, "Category change not found")
    if actor and batch.detail.get("attribution") != actor.attribution():
        raise HTTPException(404, "Category change not found")
    data = batch.detail
    if actor and data.get("group_after"):
        expected_group = data["group_after"]
        group = s.get(merchant_groups.MerchantGroup, expected_group["id"])
        if not group or group.merged_into or group.version != expected_group["version"]:
            raise HTTPException(409, "Merchant group changed; undo would overwrite newer work")
    for mid in sorted(data.get("rules_before", {data["merchant_id"]: None}), key=str):
        merchant_lock(s, mid)
    # Lock the audit too: replay is safe even for rule-only changes or missing merchant IDs.
    s.refresh(batch, with_for_update=True)
    if batch.detail.get("undone"):
        raise HTTPException(409, "This change was already undone")
    ids = [c["id"] for c in data["changes"]]
    rows = s.scalars(
        select(Transaction)
        .options(PAYMENT_COLUMNS)
        .where(Transaction.id.in_(ids))
        .order_by(Transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    expected = {c["id"]: c for c in data["changes"]}
    if len(rows) != len(ids) or any(
        t.version != expected[t.id]["after_version"]
        or category_state(t)
        != {"category": data["category"], "source": "manual", "status": "assigned"}
        for t in rows
    ):
        raise HTTPException(
            409, "A payment changed since this action; undo would overwrite newer work"
        )
    rules = {}
    before_rules = data.get("rules_before", {data["merchant_id"]: data.get("rule_before")})
    after_rules = data.get("rules_after", {data["merchant_id"]: data.get("rule_after")})
    restore_categories = {c["before"]["category"] for c in data["changes"]}
    if data["future_rule"]:
        restore_categories.update(r["category"] for r in before_rules.values() if r)
    if any(
        c and c.startswith("custom:") and not custom_categories.known(s, c)
        for c in restore_categories
    ):
        raise HTTPException(
            409, "A previous category was deleted. Choose another category for this payment."
        )
    if data["future_rule"]:
        # Match apply's rule-row order, including mixed group/merchant targets.
        keys = list(before_rules)
        locked_rules = s.scalars(
            select(Rule)
            .where(
                Rule.merchant_id.in_([k for k in keys if not k.startswith("group:")])
                | Rule.group_id.in_([k[6:] for k in keys if k.startswith("group:")])
            )
            .order_by(Rule.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        rules = {
            ("group:" + rule.group_id if rule.group_id else rule.merchant_id): rule
            for rule in locked_rules
        }
        for mid in keys:
            if rule_state(rules.get(mid)) != after_rules[mid]:
                raise HTTPException(
                    409, "The merchant rule changed; undo would overwrite newer work"
                )
    return batch, rows, rules


def undo(s, batch_id):
    return restore_snapshot(s, *undo_snapshot(s, batch_id))


def restore_snapshot(s, batch, rows, rules, actor=None):
    """Restore an already locked/validated snapshot in the caller's transaction."""
    data = batch.detail
    expected = {c["id"]: c for c in data["changes"]}
    if data["future_rule"]:
        before_rules = data.get("rules_before", {data["merchant_id"]: data.get("rule_before")})
        for mid, old in before_rules.items():
            if mid.startswith("group:"):
                s.get(merchant_groups.MerchantGroup, mid[6:]).version += 1
            rule = rules[mid]
            if old:
                rule.category = old["category"]
                rule.effective_from = (
                    datetime.fromisoformat(old["effective_from"]) if old["effective_from"] else None
                )
            else:
                s.delete(rule)
    for tx in rows:
        before = expected[tx.id]["before"]
        tx.category, tx.category_source, tx.category_status = (
            before["category"],
            before["source"],
            before["status"],
        )
        tx.version += 1
        tx.updated_at = now()
        # Restored pending work needs a fresh versioned job; old jobs are fenced.
        if tx.category_status == "pending":
            from monzo_backend.domain import enqueue

            enqueue(
                s,
                "category",
                "classify",
                f"category:{tx.id}:{tx.version}:undo",
                {"transaction_id": tx.id, "version": tx.version},
            )
    batch.detail = {**data, "undone": True}
    s.add(
        Audit(
            actor="mcp" if actor else "owner",
            action="category_batch_undone",
            detail=attributed_detail({"batch_id": batch.id, "restored": len(rows)}, actor),
        )
    )
    return {"restored": len(rows)}
