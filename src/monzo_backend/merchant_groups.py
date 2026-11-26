"""Persistent bank identities and owner-confirmed name aliases for merchant groups."""

import re
import unicodedata

from fastapi import HTTPException
from sqlalchemy import select, text, update

from monzo_backend.models import Audit, MerchantAlias, MerchantGroup, MerchantIdentity, Rule, now


def normalise(name):
    value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name or "")).strip().casefold()
    value = value.replace("’", "'")
    return (
        "" if value in {"", "unknown", "unnamed payment", "unknown merchant", "payment"} else value
    )


def lock(s):
    # A single lock keeps new identities, owner merges and rule writes ordered.
    # Ingest acquires this before payment locks, as do category changes.
    s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('merchant-groups', 0))"))


def resolve(s, merchant_id, name):
    if not merchant_id:
        return None
    lock(s)
    member = s.get(MerchantIdentity, merchant_id)
    if member:
        return s.get(MerchantGroup, member.group_id)
    alias_name = normalise(name)
    alias = s.get(MerchantAlias, alias_name) if alias_name else None
    group = (
        s.get(MerchantGroup, alias.group_id)
        if alias
        else MerchantGroup(name=name.strip() or "Unnamed merchant")
    )
    if not alias:
        s.add(group)
        s.flush()
        if alias_name:
            s.add(MerchantAlias(name=alias_name, group_id=group.id))
    else:
        group.version += 1
    s.add(MerchantIdentity(merchant_id=merchant_id, group_id=group.id))
    s.flush()
    return group


def for_merchant(s, merchant_id):
    member = s.get(MerchantIdentity, merchant_id) if merchant_id else None
    return s.get(MerchantGroup, member.group_id) if member else None


def merge(s, target_id, source_id, target_version, source_version, rule_policy):
    lock(s)
    target, source = s.get(MerchantGroup, target_id), s.get(MerchantGroup, source_id)
    if not target or not source or target_id == source_id:
        raise HTTPException(422, "Choose two different merchant groups")
    if (
        target.merged_into
        or source.merged_into
        or target.version != target_version
        or source.version != source_version
    ):
        raise HTTPException(409, "Merchant groups changed; reload before merging")
    # Group-wide rules/merges cannot collapse independently confirmed directions.
    if s.scalar(
        select(MerchantIdentity.merchant_id)
        .where(
            MerchantIdentity.group_id.in_([target_id, source_id]),
            MerchantIdentity.merchant_id.startswith("payment_group_"),
        )
        .limit(1)
    ):
        raise HTTPException(
            422, "Direction-scoped payment groups cannot be merged with merchant-wide rules"
        )
    target_rule = s.scalar(select(Rule).where(Rule.group_id == target_id))
    source_rule = s.scalar(select(Rule).where(Rule.group_id == source_id))
    if (
        target_rule
        and source_rule
        and target_rule.category != source_rule.category
        and not rule_policy
    ):
        raise HTTPException(422, "Choose which future category rule to keep")
    chosen = source_rule if rule_policy == "source" else target_rule or source_rule
    old_rules = [
        {
            "group_id": r.group_id,
            "category": r.category,
            "effective_from": r.effective_from.isoformat() if r.effective_from else None,
        }
        for r in (target_rule, source_rule)
        if r
    ]
    if chosen:
        category = chosen.category
        # The unified rule applies prospectively; historical verification is untouched.
        if not target_rule:
            target_rule = Rule(group_id=target_id, category=category)
            s.add(target_rule)
        target_rule.category, target_rule.effective_from = category, now()
    if source_rule:
        s.delete(source_rule)
    s.execute(
        update(MerchantIdentity)
        .where(MerchantIdentity.group_id == source_id)
        .values(group_id=target_id)
    )
    s.execute(
        update(MerchantAlias).where(MerchantAlias.group_id == source_id).values(group_id=target_id)
    )
    source.merged_into = target_id
    source.version += 1
    target.version += 1
    s.add(
        Audit(
            actor="owner",
            action="merchant_groups_merged",
            detail={
                "target": target_id,
                "source": source_id,
                "rules_before": old_rules,
                "rule_policy": rule_policy,
            },
        )
    )
    return {"group_id": target_id, "name": target.name}
