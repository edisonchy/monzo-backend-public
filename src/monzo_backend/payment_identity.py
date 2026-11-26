"""Bank display evidence only; never merchant grouping or category input."""

import re

from sqlalchemy import case, func

from monzo_backend.models import Transaction, now

UNKNOWN = "Unnamed payment"
REFERENCE_PATTERN = r"^[A-Za-z0-9/_-]*[0-9][A-Za-z0-9/_-]*$"


def clean(value, limit=500):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def description_label(description, scheme=None):
    value = clean(description)
    if scheme == "monzo_paid" and value.casefold() == "extra":
        return "Monzo Extra", "description"
    if value and re.fullmatch(REFERENCE_PATTERN, value):
        return "Payment reference: " + value[:481], "reference"
    return (value, "description") if value else (None, None)


def apply_identity(tx, data, *, check_money=True):
    """Safe for backfill: validate identity/money and change display fields only.

    Missing fields do not erase previously observed bank names. A new explicit
    merchant identity invalidates the previous merchant display evidence.
    """
    if data.get("id") != tx.id or data.get("account_id", tx.account_id) != tx.account_id:
        raise ValueError("Payment identity mismatch")
    if check_money and (data.get("amount") != tx.amount or data.get("currency") != tx.currency):
        raise ValueError("Payment identity money mismatch")
    merchant = data.get("merchant")
    merchant = merchant if isinstance(merchant, dict) else {}
    cp = data.get("counterparty")
    cp = cp if isinstance(cp, dict) else {}
    name = clean(merchant.get("name"), 500)
    counterparty = clean(cp.get("name"), 500) or clean(cp.get("preferred_name"), 500)
    if counterparty:
        tx.counterparty_name = counterparty
    if (
        merchant.get("id")
        and merchant["id"] != tx.merchant_id
        and tx.payment_name_source == "merchant"
    ):
        tx.payment_name, tx.payment_name_source = None, None
    if name:
        tx.payment_name, tx.payment_name_source = name, "merchant"
    elif tx.payment_name_source == "merchant" and not (
        merchant.get("id") and merchant["id"] != tx.merchant_id
    ):
        pass
    elif tx.counterparty_name:
        tx.payment_name, tx.payment_name_source = tx.counterparty_name, "counterparty"
    else:
        label, source = description_label(data.get("description"), data.get("scheme"))
        if label:
            tx.payment_name, tx.payment_name_source = label, source
    tx.identity_checked_at = now()


def payment_name(tx):
    return (
        tx.payment_name
        or clean(tx.merchant_name, 500)
        or description_label(tx.description, (tx.reporting_facts or {}).get("scheme"))[0]
        or UNKNOWN
    )


# Legacy rows have useful descriptions before the evidence-only backfill. The
# same expression is used by filters, reports, search and quality counts.
_desc = func.left(func.btrim(func.regexp_replace(Transaction.description, r"\s+", " ", "g")), 500)
_legacy_description = case(
    (
        (Transaction.reporting_facts["scheme"].astext == "monzo_paid")
        & (func.lower(_desc) == "extra"),
        "Monzo Extra",
    ),
    (_desc.op("~")(REFERENCE_PATTERN), "Payment reference: " + func.left(_desc, 481)),
    else_=func.nullif(_desc, ""),
)
PAYMENT_NAME = func.coalesce(
    func.nullif(Transaction.payment_name, ""),
    func.nullif(func.btrim(Transaction.merchant_name), ""),
    _legacy_description,
    UNKNOWN,
)
