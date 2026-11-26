"""Owner-confirmed business groups without fabricated bank merchant IDs."""

from sqlalchemy import select

from monzo_backend import merchant_groups
from monzo_backend.models import PaymentGroupMatcher
from monzo_backend.payment_purpose import counterparty_key

PREFIX = "payment_group_"


def evidence(data):
    cp = data.get("counterparty") or {}
    metadata = data.get("metadata") or {}
    return {
        "direction": "out" if data["amount"] < 0 else "in" if data["amount"] > 0 else "zero",
        "scheme": data.get("scheme") or "",
        "name": merchant_groups.normalise(cp.get("name")) if isinstance(cp, dict) else "",
        "counterparty_key": counterparty_key(data),
        "plan": metadata.get("plan_definition_id") if isinstance(metadata, dict) else None,
        "description": merchant_groups.normalise(data.get("description")),
    }


def confirmed_matcher(data):
    facts = evidence(data)
    if facts["scheme"] == "monzo_paid" and facts["plan"] and facts["description"]:
        return {k: facts[k] for k in ("direction", "scheme", "plan", "description")}
    if not facts["name"]:
        raise ValueError("No stable business name or subscription evidence")
    keys = ["direction", "scheme", "name"]
    if facts["counterparty_key"]:
        keys.append("counterparty_key")
    return {k: facts[k] for k in keys}


def resolve(s, account_id, data):
    # Same ordering as merchant registration, category edits and owner backfills.
    merchant_groups.lock(s)
    candidates = s.scalars(
        select(PaymentGroupMatcher).where(
            PaymentGroupMatcher.account_id == account_id, PaymentGroupMatcher.active.is_(True)
        )
    )
    facts = None
    matches = set()
    for matcher in candidates:
        if facts is None:
            facts = evidence(data)
        if matcher.evidence and all(facts.get(k) == v for k, v in matcher.evidence.items()):
            matches.add(matcher.identity_id)
    # Ambiguous matches never silently choose a group/category.
    return next(iter(matches)) if len(matches) == 1 else None
