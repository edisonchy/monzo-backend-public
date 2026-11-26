import hashlib
import json
import random
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from monzo_backend import merchant_groups
from monzo_backend.card_checks import exclude_card_check, is_card_check
from monzo_backend.category_policy import CATEGORIES
from monzo_backend.config import settings
from monzo_backend.db import session
from monzo_backend.http import ServiceError
from monzo_backend.models import (
    Account,
    Attempt,
    Audit,
    Control,
    Incident,
    Job,
    Notification,
    Rule,
    Transaction,
    now,
    uid,
)
from monzo_backend.payment_identity import apply_identity
from monzo_backend.payment_purpose import apply_known_purpose
from monzo_backend.spending import bank_facts, verified_pot_transfer

TRUSTED_MAPPINGS = {c: c for c in CATEGORIES if c not in {"general", "income", "transfers"}}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def timestamp(value):
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result.astimezone(UTC)


def enqueue(s, queue, kind, key, payload):
    job_id = uid()
    s.execute(
        insert(Job)
        .values(id=job_id, queue=queue, kind=kind, dedupe_key=key, payload=payload)
        .on_conflict_do_nothing(index_elements=[Job.dedupe_key])
    )
    return s.scalar(select(Job.id).where(Job.dedupe_key == key))


def notify(s, key, subject, body):
    s.execute(
        insert(Notification)
        .values(id=uid(), event_key=key, subject=subject, body=body)
        .on_conflict_do_nothing(index_elements=[Notification.event_key])
    )


def ingest(s, account_id, data, *, allow_ai=True):
    if data.get("account_id", account_id) != account_id:
        raise ValueError("Account mismatch")
    tx_id = data["id"]
    merchant = data.get("merchant") or {}
    merchant_id = merchant.get("id") if isinstance(merchant, dict) else merchant
    merchant_name = merchant.get("name", "") if isinstance(merchant, dict) else ""
    merchant_groups.resolve(s, merchant_id, merchant_name[:500])
    from monzo_backend.payment_groups import resolve as resolve_payment_group

    payment_group_identity = resolve_payment_group(s, account_id, data)
    # Deliberately do not infer transfer/refund/income semantics from amount sign.
    values = dict(
        account_id=account_id,
        reporting_facts=bank_facts(data),
        reporting_checked_at=now(),
        amount=data["amount"],
        currency=data["currency"],
        created_at=timestamp(data["created"]),
        settled_at=timestamp(data.get("settled")),
        declined=bool(data.get("decline_reason")),
        description=data.get("description", "")[:2000],
        merchant_id=merchant_id,
        merchant_name=merchant_name[:500],
        bank_category=data.get("category", "general"),
    )
    if type(values["amount"]) is not int or len(values["currency"]) != 3:
        raise ValueError("Invalid money representation")
    classification_input = {
        k: values[k] for k in ("merchant_id", "merchant_name", "bank_category", "declined")
    }
    fingerprint = digest(classification_input)
    s.execute(
        insert(Transaction)
        .values(id=tx_id, input_hash=fingerprint, **values)
        .on_conflict_do_nothing(index_elements=[Transaction.id])
    )
    tx = s.scalar(select(Transaction).where(Transaction.id == tx_id).with_for_update())
    if tx.account_id != account_id:
        raise ValueError("Transaction account changed")
    account_kind = s.get(Account, account_id).kind
    was_pot = verified_pot_transfer(tx, account_kind)
    revoke_check = tx.transaction_type == "card_verification" and (
        values["amount"] != 0
        or values["declined"]
        or any(getattr(tx, k) != values[k] for k in ("currency", "merchant_id", "merchant_name"))
    )
    group_changed = tx.payment_group_identity != payment_group_identity
    tx.payment_group_identity = payment_group_identity
    changed = tx.input_hash != fingerprint or revoke_check or group_changed
    if revoke_check:
        tx.transaction_type = "unknown"
        tx.category, tx.category_source, tx.category_status = None, None, "pending"
        s.add(
            Audit(
                actor="system",
                action="card_verification_revoked",
                detail={
                    "transaction_id": tx.id,
                    "reason": "bank_payment_changed",
                },
            )
        )
    # Display evidence is independent of the classification fingerprint.
    apply_identity(tx, data, check_money=False)
    for k, v in values.items():
        setattr(tx, k, v)
    tx.updated_at = now()
    changed = changed or was_pot != verified_pot_transfer(tx, account_kind)
    if changed:
        tx.version += 1
        tx.input_hash = fingerprint
        if tx.category_source != "manual":
            tx.category = None
            tx.category_status = "pending"
    apply_known_purpose(s, tx, data)
    if is_card_check(tx):
        exclude_card_check(tx)
    else:
        assign_pot_transfer(s, tx)
    if tx.category_source != "manual" and tx.category_status == "pending":
        if not allow_ai:
            tx.version += 1  # Fence any previously queued/in-flight classification.
            decision = deterministic(s, tx)
            tx.category = decision["category"] if decision else None
            tx.category_source = decision["source"] if decision else "review"
            tx.category_status = decision["status"] if decision else "needs_review"
            return tx
        enqueue(
            s,
            "category",
            "classify",
            f"category:{tx_id}:{tx.version}:v1",
            {"transaction_id": tx_id, "version": tx.version},
        )
    return tx


def control(s):
    return s.scalar(select(Control).where(Control.id == 1).with_for_update())


def pause(s, ctl, job, code):
    if not ctl.incident_id:
        incident = Incident(id=uid(), job_id=job.id if job else None, error_code=code)
        s.add(incident)
        ctl.incident_id = incident.id
        notify(
            s,
            f"paused:{incident.id}",
            "Monzo categorisation paused",
            f"Incident {incident.id}. Error: {code}. Transaction collection remains active. "
            "Fix the cause, then request Resume using the private owner API.",
        )
    elif ctl.state == "recovering":
        notify(
            s,
            f"recovery-failed:{ctl.incident_id}:{ctl.recovery_attempt}",
            "Monzo recovery failed",
            f"Incident {ctl.incident_id}. Error: {code}. Processing remains paused.",
        )
    ctl.state, ctl.changed_at = "paused", now()
    if job:
        job.state, job.error_code = "failed", code
        job.lease_token, job.lease_until = None, None
    s.add(Audit(actor="worker", action="categorisation_paused", detail={"error": code}))


def resume(s):
    ctl = control(s)
    if ctl.state != "paused":
        return ctl.state
    job = s.get(Job, ctl.active_job_id) if ctl.active_job_id else None
    if job is None:
        # A manual pause without active work has nothing to probe.
        resolve(s, ctl)
    else:
        ctl.state = "recovering"
        ctl.recovery_attempt += 1
        job.state, job.attempts = "pending", 0
        job.available_at = now()
        job.lease_token, job.lease_until = None, None
    ctl.changed_at = now()
    s.add(Audit(actor="owner", action="resume_requested", detail={"incident_id": ctl.incident_id}))
    return ctl.state


def resolve(s, ctl):
    if ctl.incident_id:
        incident = s.get(Incident, ctl.incident_id)
        incident.resolved_at = now()
        notify(
            s,
            f"recovered:{incident.id}",
            "Monzo categorisation resumed",
            f"Incident {incident.id} resolved. Processing has resumed; the backlog may still be draining.",
        )
    ctl.state, ctl.active_job_id, ctl.incident_id = "running", None, None
    ctl.changed_at = now()


def claim(s, queue):
    cfg = settings()
    ctl = control(s) if queue == "category" else None
    if ctl and ctl.state == "paused":
        return None
    eligible = or_(Job.state == "pending", (Job.state == "running") & (Job.lease_until < now()))
    query = select(Job).where(Job.queue == queue, eligible, Job.available_at <= now())
    if ctl and ctl.active_job_id:
        query = query.where(Job.id == ctl.active_job_id)
    if queue == "sync":
        # Preserve the short initial-history access window. Once history is imported,
        # ageing bounds targeted-fetch priority over routine reconciliation.
        query = query.order_by(
            case(
                (Job.kind == "discover", 0),
                (Job.payload["historical"].as_boolean().is_(True), 1),
                (Job.created_at < now() - timedelta(minutes=5), 2),
                (Job.kind == "fetch_transaction", 3),
                else_=4,
            )
        )
    job = s.scalar(
        query.order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1)
    )
    if job is None:
        return None
    if job.attempts >= job.payload.get("webhook_retry_limit", cfg.max_attempts):
        if ctl:
            pause(s, ctl, job, "worker_attempts_exhausted")
        else:
            fail_sync(s, job, "worker_attempts_exhausted")
        return None
    job.attempts += 1
    job.state, job.lease_token = "running", uid()
    job.lease_until = now() + timedelta(seconds=cfg.job_lease_seconds)
    if ctl:
        ctl.active_job_id = job.id
    s.flush()
    return {
        "id": job.id,
        "kind": job.kind,
        "payload": job.payload,
        "lease_token": job.lease_token,
        "recovering": bool(
            ctl
            and ctl.state == "recovering"
            and ctl.incident_id
            and s.get(Incident, ctl.incident_id).error_code.startswith(("provider_", "ai_"))
        ),
    }


def fail_sync(s, job, code):
    job.state, job.error_code = "failed", code
    job.lease_token, job.lease_until = None, None
    if job.payload.get("historical") and job.payload.get("account_id"):
        acct = s.get(Account, job.payload["account_id"])
        if acct:
            acct.history_status = "incomplete"
    if code == "monzo_reauthorisation_required":
        # The transition to this connection state already queued the incident email.
        return
    notify(
        s,
        f"sync-failed:{job.id}",
        "Monzo synchronisation needs attention",
        f"Sync job {job.id} failed: {code}. Bank data may be stale. Check the private status API.",
    )


class LostSyncLease(Exception):
    pass


def owns_sync_lease(job, work, at):
    return (
        job is not None
        and job.state == "running"
        and job.lease_token == work["lease_token"]
        and job.lease_until is not None
        and job.lease_until > at
    )


@contextmanager
def local_sync_transaction():
    """Only use for database work that can be replayed after a confirmed rollback."""
    try:
        with session() as s, s.begin():
            yield s
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in ("40001", "40P01", "55P03"):
            raise ServiceError("sync_database_contention") from exc
        raise


@contextmanager
def sync_result(work=None, *, error=None):
    """Commit local effects and job outcome together, after any bank calls."""
    claimed = work is not None and "lease_token" in work
    with local_sync_transaction() as s:
        if claimed:
            job = s.scalar(select(Job).where(Job.id == work["id"]).with_for_update())
            if not owns_sync_lease(job, work, now()):
                raise LostSyncLease
        yield s
        if claimed and not finish(s, work, error=error):
            raise LostSyncLease


def finish(s, work, result=None, error=None):
    # Lock order matches claim/resume: control before job, then transaction.
    ctl = control(s) if work["kind"] == "classify" else None
    job = s.scalar(select(Job).where(Job.id == work["id"]).with_for_update())
    if job is None or job.lease_token != work["lease_token"] or job.state != "running":
        return False  # stale worker or owner invalidated its lease
    if not ctl and not owns_sync_lease(job, work, now()):
        return False
    if error:
        job.error_code = error.code
        if ctl and (
            not error.retryable
            or job.attempts >= settings().max_attempts
            or ctl.state == "recovering"
        ):
            pause(s, ctl, job, error.code)
        elif not ctl and (
            not error.retryable
            or job.attempts >= job.payload.get("webhook_retry_limit", settings().max_attempts)
        ):
            fail_sync(s, job, error.code)
        else:
            delay = max(error.retry_after, settings().retry_base_seconds * 2 ** (job.attempts - 1))
            job.available_at = now() + timedelta(seconds=delay + random.uniform(0, 2))
            job.state = "pending"
            if ctl and ctl.state != "recovering":
                ctl.state = "retrying"
        job.lease_token, job.lease_until = None, None
        return True
    if ctl and result and result.get("skipped"):
        job.state, job.error_code = "completed", None
        job.lease_token, job.lease_until = None, None
        # A stale result must not certify recovery of a broken provider.
        ctl.active_job_id = None
        if not work["recovering"] and ctl.state != "paused":
            ctl.state = "running"
        return True
    if ctl:
        tx = s.scalar(
            select(Transaction)
            .where(Transaction.id == job.payload["transaction_id"])
            .with_for_update()
        )
        s.add(
            Attempt(
                job_id=job.id,
                transaction_id=tx.id,
                input_version=job.payload["version"],
                model=result.get("model", ""),
                result=result,
            )
        )
        if (
            tx.version == job.payload["version"]
            and tx.category_source != "manual"
            and not is_card_check(tx)
        ):
            tx.category = result.get("category")
            tx.category_source = result["source"]
            tx.category_status = result["status"]
        if work["recovering"] and not result.get("provider_verified"):
            # A manual override/rule must not conceal the provider failure under investigation.
            pause(s, ctl, job, "recovery_requires_provider_probe")
            return True
        resolve(s, ctl)
        ctl.last_success_at = now()
    job.state, job.error_code = "completed", None
    job.lease_token, job.lease_until = None, None
    return True


def assign_pot_transfer(s, tx):
    """Resolve verified own-pot movements without AI; preserve explicit owner decisions."""
    account = s.get(Account, tx.account_id)
    if (
        not account
        or is_card_check(tx)
        or not verified_pot_transfer(tx, account.kind)
        or tx.declined
        or tx.category_source == "manual"
        or (tx.category, tx.category_source, tx.category_status)
        == ("transfers", "monzo", "assigned")
    ):
        return False
    tx.category, tx.category_source, tx.category_status = "transfers", "monzo", "assigned"
    tx.version += 1  # Fence stale worker results and owner approvals.
    tx.updated_at = now()
    s.add(
        Audit(
            actor="system",
            action="pot_transfer_classified",
            detail={
                "transaction_id": tx.id,
                "category": "transfers",
                "version": tx.version,
                "evidence": "bank_scheme_and_both_pot_references",
            },
        )
    )
    return True


def deterministic(s, tx):
    if is_card_check(tx):
        return {"category": None, "source": "owner_verification", "status": "excluded"}
    if tx.category_source == "manual":
        return {"category": tx.category, "source": "manual", "status": "assigned"}
    if tx.declined:
        return {"category": None, "source": "excluded", "status": "excluded"}
    account = s.get(Account, tx.account_id)
    if account and verified_pot_transfer(tx, account.kind):
        return {"category": "transfers", "source": "monzo", "status": "assigned"}
    if tx.category_status == "assigned" and tx.category_source in ("rule", "monzo"):
        return {"category": tx.category, "source": tx.category_source, "status": "assigned"}
    group = merchant_groups.for_merchant(s, tx.category_identity)
    group_rule = (
        s.scalar(select(Rule).where(Rule.group_id == group.id))
        if group and not tx.payment_group_identity
        else None
    )
    if group_rule and (
        group_rule.effective_from is None or tx.created_at >= group_rule.effective_from
    ):
        return {"category": group_rule.category, "source": "rule", "status": "assigned"}
    rule = (
        s.scalar(select(Rule).where(Rule.merchant_id == tx.category_identity))
        if tx.category_identity
        else None
    )
    if rule and (rule.effective_from is None or tx.created_at >= rule.effective_from):
        return {"category": rule.category, "source": "rule", "status": "assigned"}
    if tx.bank_category in TRUSTED_MAPPINGS:
        return {
            "category": TRUSTED_MAPPINGS[tx.bank_category],
            "source": "monzo",
            "status": "assigned",
        }
    return None
