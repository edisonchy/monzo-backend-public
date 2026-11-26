"""Persist native-search results by cache key; never fetch cited URLs."""

import hashlib
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import insert

from monzo_backend.category_policy import RESEARCH_INSTRUCTIONS
from monzo_backend.db import session
from monzo_backend.domain import CATEGORIES
from monzo_backend.http import ServiceError, request
from monzo_backend.models import MerchantResearch, now
from monzo_backend.provider_usage import tracked_request


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: str | None
    needs_review: bool
    summary: str = Field(max_length=2000)
    source_urls: list[str] = Field(max_length=10)

    @field_validator("category")
    @classmethod
    def allowed(cls, value):
        if value is not None and value not in CATEGORIES:
            raise ValueError("Unknown category")
        return value


def merchant_key(name):
    # Deliberately independent of model, bank category and prompt version.
    return hashlib.sha256(name.encode()).hexdigest()


def saved_research(key, *, db_session=None):
    if db_session is None:
        with session() as s:
            return saved_research(key, db_session=s)
    row = db_session.get(MerchantResearch, key)
    if row:
        return {
            "key": row.key,
            "finding": row.finding,
            "model": row.model,
            "created_at": row.created_at,
        }
    return None


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    url = urlsplit(value)
    return (
        url.scheme in ("https", "http")
        and bool(url.hostname)
        and not url.username
        and not url.password
    )


def research(cfg, key, merchant, *, work=None):
    saved = saved_research(key)
    if saved:
        return saved, True, {}
    result = tracked_request(
        request,
        "web_search",
        work,
        "POST",
        cfg.proxy_url.rstrip("/") + "/responses",
        timeout_seconds=cfg.search_timeout,
        headers={"Authorization": "Bearer " + cfg.proxy_key.get_secret_value()},
        json={
            "model": cfg.model,
            "stream": False,
            "max_output_tokens": 1200,
            "tools": [{"type": "web_search", "search_context_size": "low"}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "instructions": RESEARCH_INSTRUCTIONS,
            "input": merchant,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "merchant_finding",
                    "strict": True,
                    "schema": Finding.model_json_schema(),
                }
            },
        },
    )
    try:
        if result["status"] != "completed":
            raise ValueError()
        outputs = result["output"]
        searches = [
            x
            for x in outputs
            if x.get("type") == "web_search_call" and x.get("status") == "completed"
        ]
        if not searches:
            raise ValueError()
        parts = [
            p
            for x in outputs
            if x.get("type") == "message"
            for p in x.get("content", [])
            if p.get("type") == "output_text"
        ]
        finding = Finding.model_validate_json("".join(p["text"] for p in parts))
        observed = {
            a["url"]
            for p in parts
            for a in p.get("annotations", [])
            if a.get("type") == "url_citation" and public_url(a.get("url"))
        }
        observed.update(
            a["url"]
            for x in searches
            for a in x.get("action", {}).get("sources", [])
            if public_url(a.get("url"))
        )
        if any(not public_url(url) or url not in observed for url in finding.source_urls):
            raise ValueError()
        if finding.category and not finding.source_urls:
            raise ValueError()
        if finding.category is None:
            finding.needs_review = True
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ServiceError("ai_search_invalid_evidence") from None
    usage = {
        k: v
        for k, v in result.get("usage", {}).items()
        if k in ("input_tokens", "output_tokens", "total_tokens") and type(v) is int and v >= 0
    }
    with session() as s, s.begin():
        s.execute(
            insert(MerchantResearch)
            .values(
                key=key,
                merchant_name=merchant,
                finding=finding.model_dump(),
                model=cfg.model,
                created_at=now(),
                usage=usage,
            )
            .on_conflict_do_nothing(index_elements=[MerchantResearch.key])
        )
    return saved_research(key), False, usage
