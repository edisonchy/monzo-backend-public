import hashlib
import json
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from monzo_backend import category_policy
from monzo_backend.card_checks import is_card_check
from monzo_backend.category_policy import bank_context, classification_request, merchant_context
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.domain import CATEGORIES, deterministic
from monzo_backend.http import ServiceError, request
from monzo_backend.models import (
    MerchantDecision,
    MerchantIdentity,
    MerchantResearch,
    Transaction,
    now,
)
from monzo_backend.provider_usage import tracked_request
from monzo_backend.research import merchant_key, research, saved_research

# Bump when prompts, sanitisation or interpretation change.
POLICY_VERSION = category_policy.CLASSIFICATION_POLICY_VERSION


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: str | None
    needs_review: bool

    @field_validator("category")
    @classmethod
    def allowed(cls, value):
        if value is not None and value not in CATEGORIES:
            raise ValueError("Unknown category")
        return value


def decision_key(tx, cfg):
    return hashlib.sha256(
        json.dumps(
            [
                tx.merchant_name,
                tx.bank_category,
                cfg.model,
                cfg.proxy_url,
                POLICY_VERSION,
                CATEGORIES,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def payment_group_key(tx, cfg, policy_version):
    return hashlib.sha256(
        json.dumps(
            [
                "payment-group-v1",
                tx.payment_group_identity,
                bank_context(tx.bank_category),
                cfg.model,
                cfg.proxy_url,
                policy_version,
                CATEGORIES,
            ]
        ).encode()
    ).hexdigest()


def cache_keys(s, tx, cfg):
    """Group/context first, then compatible legacy entries without reclassifying history."""
    if tx.payment_group_identity:
        # Freeze the original research identity so old evidence remains displayable.
        return [payment_group_key(tx, cfg, POLICY_VERSION)], [
            payment_group_key(tx, cfg, "merchant-v1")
        ]
    member = s.get(MerchantIdentity, tx.category_identity) if tx.category_identity else None
    if not member:
        return [decision_key(tx, cfg)], [merchant_key(tx.merchant_name)]
    context = bank_context(tx.bank_category)
    identity = ["group-v1", member.group_id, context]
    decision = hashlib.sha256(
        json.dumps(
            [identity, cfg.model, cfg.proxy_url, POLICY_VERSION, CATEGORIES], sort_keys=True
        ).encode()
    ).hexdigest()
    research = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    peers = s.execute(
        select(Transaction.merchant_name, Transaction.bank_category)
        .join(MerchantIdentity, MerchantIdentity.merchant_id == Transaction.category_identity)
        .where(MerchantIdentity.group_id == member.group_id)
        .distinct()
    ).all()
    compatible = [
        SimpleNamespace(merchant_name=name, bank_category=bank)
        for name, bank in peers
        if bank_context(bank) == context
    ]
    return (
        list(dict.fromkeys([decision] + [decision_key(peer, cfg) for peer in compatible])),
        list(dict.fromkeys([research] + [merchant_key(peer.merchant_name) for peer in compatible])),
    )


def cached_decision(s, keys):
    rows = [
        row
        for key in keys
        if (row := s.get(MerchantDecision, key))
        and (row.expires_at is None or row.expires_at > now())
    ]
    if not rows:
        return None
    if rows[0].key == keys[0]:
        return rows[0]
    # Legacy aliases may have disagreed. Never select an arbitrary suggestion.
    return rows[0] if len({json.dumps(row.decision, sort_keys=True) for row in rows}) == 1 else None


def cached_research(keys, *, db_session=None):
    if db_session is None:
        with session() as s:
            return cached_research(keys, db_session=s)
    rows = [row for key in keys if (row := saved_research(key, db_session=db_session))]
    if not rows:
        return None
    if rows[0]["key"] == keys[0]:
        return rows[0]
    outcomes = {
        (row["finding"].get("category"), row["finding"].get("needs_review")) for row in rows
    }
    return rows[0] if len(outcomes) == 1 else None


def current_research_keys(keys):
    return [research_policy_key(key, category_policy.RESEARCH_POLICY_VERSION) for key in keys]


def research_policy_key(key, version):
    return hashlib.sha256(json.dumps(["research-policy", version, key]).encode()).hexdigest()


def displayed_research(s, tx, cfg):
    keys = cache_keys(s, tx, cfg)[1]
    for version in (
        category_policy.RESEARCH_POLICY_VERSION,
        *category_policy.PREVIOUS_RESEARCH_POLICY_VERSIONS,
    ):
        version_keys = [research_policy_key(key, version) for key in keys]
        if s.scalar(
            select(MerchantResearch.key).where(MerchantResearch.key.in_(version_keys)).limit(1)
        ):
            return cached_research(version_keys, db_session=s)
    return cached_research(keys, db_session=s)


def classify(work):
    cfg = settings()
    with session() as s:
        tx = s.get(Transaction, work["payload"]["transaction_id"])
        # Owner edits must not remove the last provider recovery probe.
        # finish() still fences the old version and preserves the owner's category.
        if tx is None or (
            tx.version != work["payload"]["version"]
            and not (work["recovering"] and (tx.category_source == "manual" or is_card_check(tx)))
        ):
            return {"skipped": "stale_input"}
        rule = deterministic(s, tx)
        merchant = merchant_context(tx.merchant_name)
        bank_category = bank_context(tx.bank_category)[:100]
        decision_keys, research_keys = cache_keys(s, tx, cfg)
        research_keys = current_research_keys(research_keys)
        research_key = research_keys[0]
        # Hash full original inputs, not truncated/redacted text: redaction must not
        # collapse different merchants into one cache entry. No fuzzy matching.
        cache_key = decision_keys[0]
    if rule and not work["recovering"]:
        return rule
    if rule and rule["source"] == "owner_verification":
        # Probe provider health without sending or caching card-check details.
        merchant, bank_category = "", "unknown"
    if not cfg.ai_enabled:
        if work["recovering"]:
            raise ServiceError("ai_disabled_during_recovery", False)
        return {"category": None, "source": "review", "status": "needs_review"}
    if not cfg.model or not cfg.proxy_key.get_secret_value():
        raise ServiceError("ai_configuration_missing", False)
    if not merchant and not work["recovering"]:
        return {"category": None, "source": "review", "status": "needs_review"}
    if cfg.web_search_enabled and merchant and not rule and not work["recovering"]:
        saved = cached_research(research_keys)
        if saved:
            return research_result(saved, cfg, cache_hit=True)
    if not work["recovering"]:
        with session() as s:
            cached = cached_decision(s, decision_keys)
            if cached and (cached.expires_at is None or cached.expires_at > now()):
                decision = Decision.model_validate(cached.decision)
                if cfg.web_search_enabled and (decision.category is None or decision.needs_review):
                    return search_result(cfg, research_key, merchant, work)
                return decision_result(decision, cfg, cache_hit=True)
    result = tracked_request(
        request,
        "classification",
        work,
        "POST",
        cfg.proxy_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {cfg.proxy_key.get_secret_value()}"},
        json=classification_request(merchant, bank_category, cfg.model),
    )
    try:
        decision = Decision.model_validate_json(result["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise ServiceError("ai_invalid_output") from None
    if merchant:
        with session() as s, s.begin():
            values = {
                "key": cache_key,
                "decision": decision.model_dump(),
                "expires_at": None,
            }
            s.execute(
                insert(MerchantDecision)
                .values(**values)
                .on_conflict_do_update(index_elements=[MerchantDecision.key], set_=values)
            )
    if rule:
        return {**rule, "provider_verified": True, "model": cfg.model}
    if cfg.web_search_enabled and merchant and (decision.category is None or decision.needs_review):
        output = search_result(cfg, research_key, merchant, work)
        output["provider_verified"] = (
            True  # The classification call above also probed the provider.
        )
    else:
        output = decision_result(decision, cfg, cache_hit=False)
    # Record only numeric usage counters, never provider payloads or identifiers.
    usage = result.get("usage")
    if isinstance(usage, dict):
        output["usage"] = {
            k: v
            for k, v in usage.items()
            if k in ("prompt_tokens", "completion_tokens", "total_tokens")
            and type(v) is int
            and v >= 0
        }
    return output


def decision_result(decision, cfg, *, cache_hit):
    return {
        "category": decision.category,
        "source": "ai",
        "model": cfg.model,
        "provider_verified": not cache_hit,
        "cache_hit": cache_hit,
        "policy_version": POLICY_VERSION,
        "status": "assigned"
        if cfg.ai_auto_accept and decision.category and not decision.needs_review
        else "needs_review",
    }


def search_result(cfg, key, merchant, work=None):
    saved, hit, usage = research(cfg, key, merchant, work=work)
    output = research_result(saved, cfg, cache_hit=hit)
    if usage:
        output["search_usage"] = usage
    return output


def research_result(saved, cfg, *, cache_hit):
    finding = saved["finding"]
    decision = Decision(category=finding["category"], needs_review=finding["needs_review"])
    result = decision_result(decision, cfg, cache_hit=cache_hit)
    result.update(research_key=saved["key"], model=saved["model"], web_researched=True)
    # Web identity matching is a suggestion even if general AI auto-accept is enabled.
    result["status"] = "needs_review"
    return result
