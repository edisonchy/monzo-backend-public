"""Conservative, bank-evidenced transfer exclusions; independent of AI categories."""

import hashlib
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import case, select

from monzo_backend.models import Account, Transaction, now
from monzo_backend.payment_identity import PAYMENT_NAME, payment_name
from monzo_backend.payment_purpose import confirmed_purpose

ACCOUNT_KINDS = ("uk_retail", "uk_monzo_flex")
COMBINED = "combined"


def eligible_accounts():
    return select(Account.id).where(Account.kind.in_(ACCOUNT_KINDS))


def verified_pot_transfer(tx, account_kind):
    facts = tx.reporting_facts or {}
    return bool(
        account_kind == "uk_retail"
        and facts.get("scheme") == "uk_retail_pot"
        and facts.get("pot_ref")
        and facts.get("pot_account_ref")
    )


def pot_name(amount):
    if amount < 0:
        return "Transfer to savings"
    if amount > 0:
        return "Transfer from savings"
    return "Savings transfer"


# Match the Python evidence predicate for grouping, drilldowns and text search.
POT_EVIDENCE = (
    Transaction.account_id.in_(select(Account.id).where(Account.kind == "uk_retail"))
    & (Transaction.reporting_facts["scheme"].astext == "uk_retail_pot")
    & (Transaction.reporting_facts["pot_ref"].astext != "")
    & (Transaction.reporting_facts["pot_account_ref"].astext != "")
)
DISPLAY_MERCHANT = case(
    (
        POT_EVIDENCE,
        case(
            (Transaction.amount < 0, pot_name(-1)),
            (Transaction.amount > 0, pot_name(1)),
            else_=pot_name(0),
        ),
    ),
    else_=PAYMENT_NAME,
)


def bank_facts(data):
    """Persist only the evidence required for reporting, never the raw bank payload."""
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    facts = {}
    for key, value in {
        "scheme": data.get("scheme"),
        "flex_type": metadata.get("flex_transaction_type"),
    }.items():
        if isinstance(value, str) and 0 < len(value) <= 100:
            facts[key] = value
    for target, source in {
        "ledger_ref": "ledger_insertion_id",
        "flex_ref": "monzo_flex_id",
        "pot_ref": "pot_id",
        "pot_account_ref": "pot_account_id",
    }.items():
        value = metadata.get(source)
        if isinstance(value, str) and 0 < len(value) <= 512:
            facts[target] = hashlib.sha256(value.encode()).hexdigest()
    return facts


def apply_bank_facts(tx, data):
    """Metadata-only backfill: never reclassify, enqueue jobs or change manual decisions."""
    if data.get("id") != tx.id or data.get("account_id") != tx.account_id:
        raise ValueError("Reporting evidence identity mismatch")
    # Do not attach fresh evidence to a stale financial snapshot. Normal sync will resolve it.
    if data.get("amount") != tx.amount or data.get("currency") != tx.currency:
        raise ValueError("Reporting evidence money mismatch")
    tx.reporting_facts = bank_facts(data)
    tx.reporting_checked_at = now()


def exclusions(s, account_ids=None):
    """Return verifiable exclusions across all dates, so period boundaries do not break pairs.

    Flex pairs require a shared bank ledger event, Flex facility and event type, exactly
    two opposite legs on Current/Flex, equal currency/amount and a bounded time gap.
    Ambiguous or missing pairs remain included. Pot movements have their own explicit
    bank scheme plus both pot references. Names, categories and amount alone prove nothing.
    """
    rows = s.execute(
        select(Transaction, Account.kind)
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Account.kind.in_(ACCOUNT_KINDS),
            Account.id.in_(account_ids) if account_ids is not None else True,
            Transaction.reporting_facts["scheme"].astext.in_(("monzo_flex", "uk_retail_pot")),
        )
    ).all()
    result, groups = {}, defaultdict(list)
    own_rows = s.scalars(
        select(Transaction).where(
            Transaction.account_id.in_(eligible_accounts()),
            Transaction.account_id.in_(account_ids) if account_ids is not None else True,
            Transaction.purpose_confirmation.is_not(None),
            Transaction.declined.is_(False),
        )
    )
    for tx in own_rows:
        if (confirmed_purpose(tx) or {}).get("purpose") == "own_account":
            result[tx.id] = {"reason": "own_account_transfer", "matched_transaction_id": None}

    for tx, kind in rows:
        facts = tx.reporting_facts or {}
        if facts.get("scheme") == "uk_retail_pot":
            if verified_pot_transfer(tx, kind):
                result[tx.id] = {"reason": "pot_transfer", "matched_transaction_id": None}
        elif all(facts.get(k) for k in ("ledger_ref", "flex_ref", "flex_type")):
            key = (facts["ledger_ref"], facts["flex_ref"], facts["flex_type"], tx.currency)
            groups[key].append((tx, kind))
    for pair in groups.values():
        if len(pair) != 2:
            continue
        (a, ak), (b, bk) = pair
        if (
            {ak, bk} != set(ACCOUNT_KINDS)
            or a.amount == 0
            or a.amount != -b.amount
            or a.declined
            or b.declined
            or not a.settled_at
            or not b.settled_at
            or abs(a.created_at - b.created_at) > timedelta(days=1)
        ):
            continue
        reason = (
            "flex_repayment"
            if a.reporting_facts["flex_type"] == "repayment"
            else "internal_transfer"
        )
        result[a.id] = {"reason": reason, "matched_transaction_id": b.id}
        result[b.id] = {"reason": reason, "matched_transaction_id": a.id}
    return result


def display_payment_name(tx, excluded=None):
    if (excluded or {}).get("reason") == "pot_transfer":
        return pot_name(tx.amount)
    return payment_name(tx)
