"""Private storage/service for bounded MCP category edits. No HTTP or MCP exposure.

Preparation functions participate in the caller's transaction. commit_operation owns
its transaction and returns only after commit; receipt lookup labels the revision
as current, since dashboard revision triggers run at commit. The future command
listener must authenticate/scope-check before constructing CategoryActor.
"""

import json
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text, update

from monzo_backend import category_changes as changes
from monzo_backend.card_checks import is_card_check
from monzo_backend.db import session
from monzo_backend.models import (
    Account,
    CategoryOperation,
    CategoryWriteControl,
    DashboardRevision,
    Transaction,
    now,
    uid,
)
from monzo_backend.spending import ACCOUNT_KINDS

MAX_BYTES = 60000  # Leave room for the protocol envelope inside the 64 KiB limit.
MAX_BATCH = 100


class OperationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def bounded(value):
    if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_BYTES:
        raise OperationError("result_too_large")
    return value


def timeouts(s):
    s.execute(text("SET LOCAL lock_timeout = '1s'"))
    s.execute(text("SET LOCAL statement_timeout = '5s'"))


def write_control(s):
    """Shared row lock is held through commit; disable takes an exclusive lock."""
    row = s.scalar(
        select(CategoryWriteControl)
        .where(CategoryWriteControl.id == 1)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if not row or not row.enabled:
        raise OperationError("writes_disabled")


def set_writes_enabled(s, enabled):
    timeouts(s)
    row = s.scalar(
        select(CategoryWriteControl)
        .where(CategoryWriteControl.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise OperationError("control_missing")
    row.enabled = enabled


def owned(s, actor, operation_id, lock=False):
    try:
        operation_id = str(UUID(operation_id))
    except (ValueError, TypeError, AttributeError):
        raise OperationError("operation_not_found") from None
    q = select(CategoryOperation).where(
        CategoryOperation.id == operation_id,
        CategoryOperation.issuer == actor.issuer,
        CategoryOperation.owner_subject == actor.subject,
        CategoryOperation.authorized_client == actor.client,
        CategoryOperation.origin == "mcp",
    )
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    op = s.scalar(q)
    if op is None:
        raise OperationError("operation_not_found")
    return op


def create(s, actor, kind, command, source_batch_id=None, *, schema_version=1):
    timestamp = now()
    op = CategoryOperation(
        id=uid(),
        schema_version=schema_version,
        issuer=actor.issuer,
        owner_subject=actor.subject,
        authorized_client=actor.client,
        origin="mcp",
        kind=kind,
        command=bounded(command),
        state="prepared",
        created_at=timestamp,
        expires_at=timestamp + timedelta(minutes=15),
        source_batch_id=source_batch_id,
    )
    s.add(op)
    s.flush()
    return {
        "operation_id": op.id,
        "kind": kind,
        "state": "prepared",
        "expires_at": op.expires_at.isoformat(),
        **command["summary"],
    }


def prepare_change(s, actor, transaction_id, choice):
    """Freeze exactly one payment; merchant scopes and rules belong to Gate F."""
    if choice.scope != "this" or choice.future_rule or choice.group_merchant:
        raise OperationError("unsupported_scope")
    timeouts(s)
    write_control(s)
    tx, _, targets, payload, counts = changes.snapshot(s, transaction_id, choice, lock=True)
    summary = {
        "transaction_id": tx.id,
        "previous": changes.category_state(tx),
        "category": choice.category,
        "expected_version": tx.version,
        "verified_replaced": bool(counts["verified_replaced_count"]),
        "pending_settlement": tx.settled_at is None,
        "changed": len(targets),
        "future_rule": False,
    }
    return create(s, actor, "apply", {"snapshot": payload, "summary": summary})


def prepare_batch(s, actor, transaction_id, merchant_id, merchant_group_id, choice):
    """Freeze a bounded merchant snapshot; later arrivals never enter its commit."""
    timeouts(s)
    write_control(s)
    changes.merchant_groups.lock(s)
    tx = s.get(Transaction, transaction_id)
    if not tx or tx.category_identity != merchant_id:
        raise OperationError("merchant_mismatch")
    group = changes.merchant_groups.for_merchant(s, merchant_id)
    if merchant_group_id and (not group or group.id != merchant_group_id or group.merged_into):
        raise OperationError("group_mismatch")
    # Dashboard future rules resolve to the persistent group, even for a single
    # merchant. Require its explicit ID rather than silently widening the target.
    if choice.future_rule and group and merchant_group_id != group.id:
        raise OperationError("group_mismatch")
    if choice.group_merchant != bool(merchant_group_id and choice.scope != "this"):
        raise OperationError("unsupported_scope")
    try:
        tx, _, targets, payload, counts = changes.snapshot(
            s, transaction_id, choice, lock=True, max_rows=MAX_BATCH
        )
    except HTTPException as exc:
        if exc.detail == "batch_too_large":
            raise OperationError("batch_too_large") from None
        raise
    target_ids = {t.id for t in targets}
    summary = {
        "transaction_id": tx.id,
        "merchant_id": merchant_id,
        "merchant_group_id": merchant_group_id,
        "scope": choice.scope,
        "category": choice.category,
        "expected_version": choice.expected_version,
        "counts": counts,
        "account_ids": sorted({r[1] for r in payload["rows"]}),
        "payments": [
            {
                "id": r[0],
                "account_id": r[1],
                "version": r[3],
                "previous": r[4],
                "will_change": r[0] in target_ids,
                "pending_settlement": r[10] is None,
            }
            for r in payload["rows"]
        ],
        "future_rule": choice.future_rule,
        "rule_target": payload["future_targets"][0] if choice.future_rule else None,
        "rules_before": payload["rules"],
        "proposed_rule": {"category": choice.category, "effective_from": "commit_time"}
        if choice.future_rule
        else None,
        "later_arrivals_excluded": True,
    }
    return create(s, actor, "apply", {"snapshot": payload, "summary": summary}, schema_version=2)


def undo_state(s, actor, batch_id):
    batch, rows, rules = changes.undo_snapshot(s, batch_id, actor)
    # Only operations created by this service can be undone here. Dashboard
    # attribution alone cannot create permission to undo an unrelated batch.
    source = s.scalar(
        select(CategoryOperation).where(
            CategoryOperation.batch_id == batch_id,
            CategoryOperation.kind == "apply",
            CategoryOperation.state == "committed",
            CategoryOperation.issuer == actor.issuer,
            CategoryOperation.owner_subject == actor.subject,
            CategoryOperation.authorized_client == actor.client,
        )
    )
    if source is None or len(rows) > MAX_BATCH:
        raise OperationError("batch_not_found")
    for row in rows:
        account = s.get(Account, row.account_id)
        if not account or account.kind not in ACCOUNT_KINDS or row.declined or is_card_check(row):
            raise OperationError("undo_conflict")
    state = [
        {
            "id": row.id,
            "account_id": row.account_id,
            "merchant_id": row.merchant_id,
            "version": row.version,
            "category": changes.category_state(row),
            "amount": row.amount,
            "currency": row.currency,
            "created_at": row.created_at.isoformat(),
            "settled_at": row.settled_at.isoformat() if row.settled_at else None,
            "transaction_type": row.transaction_type,
        }
        for row in rows
    ]
    return batch, rows, rules, state, source.schema_version


def prepare_undo(s, actor, batch_id, *, allow_batches=True):
    timeouts(s)
    write_control(s)
    batch, rows, _, state, schema_version = undo_state(s, actor, batch_id)
    if schema_version == 2 and not allow_batches:
        raise OperationError("batches_disabled")
    before = batch.detail["changes"][0]["before"] if rows else {}
    summary = {
        "batch_id": batch_id,
        "transaction_id": batch.detail["transaction_id"],
        "previous": changes.category_state(rows[0]) if rows else {},
        "restore": before,
        "resumes_classification": any(
            c["before"]["status"] == "pending" for c in batch.detail["changes"]
        ),
    }
    if schema_version == 2:
        summary.update(
            payments=[{"id": c["id"], "restore": c["before"]} for c in batch.detail["changes"]],
            rules_before=batch.detail["rules_after"] if batch.detail["future_rule"] else {},
            rules_restore=batch.detail["rules_before"] if batch.detail["future_rule"] else {},
            future_rule=batch.detail["future_rule"],
        )
    return create(
        s,
        actor,
        "undo",
        {"rows": state, "summary": summary},
        batch_id,
        schema_version=schema_version,
    )


def _commit(s, actor, operation_id, kind, *, allow_batches=True):
    """Internal transaction body. Never expose an uncommitted return value."""
    timeouts(s)
    op = owned(s, actor, operation_id, lock=True)
    if kind not in {"apply", "undo"} or op.kind != kind:
        raise OperationError("operation_kind_mismatch")
    if op.state == "committed":
        return
    if op.state in {"expired", "stale"}:
        return
    if op.schema_version not in (1, 2):
        raise OperationError("unsupported_schema")
    if op.schema_version == 2 and not allow_batches:
        raise OperationError("batches_disabled")
    write_control(s)
    if op.expires_at <= now():
        op.state = "expired"
        return
    try:
        if kind == "apply":
            frozen = op.command["snapshot"]
            choice = changes.CategoryChoice.model_validate(frozen["choice"])
            tx, rules, targets, payload, counts = changes.snapshot(
                s,
                frozen["tx_id"],
                choice,
                lock=True,
                frozen_ids=[r[0] for r in frozen["rows"]] if op.schema_version == 2 else None,
                max_rows=MAX_BATCH if op.schema_version == 2 else None,
            )
            if payload != frozen:
                op.state = "stale"
                return
            if not targets and not choice.future_rule:
                result = {"batch_id": None, "changed": 0, "future_rule": False}
            else:
                result = changes.apply_snapshot(s, tx, rules, targets, payload, counts, actor)
            versions = (
                [{"id": row.id, "version": row.version} for row in targets]
                if op.schema_version == 2
                else [{"id": tx.id, "version": tx.version}]
            )
            if op.schema_version == 2:
                result.update(
                    rule_target=op.command["summary"]["rule_target"],
                    later_arrivals_excluded=True,
                    counts=counts,
                    rules_before={key: frozen["rules"][key] for key in frozen["future_targets"]}
                    if choice.future_rule
                    else {},
                    rules_after={
                        key: changes.rule_state(rules.get(key)) for key in frozen["future_targets"]
                    }
                    if choice.future_rule
                    else {},
                )
        else:
            batch, rows, rules, state, _ = undo_state(s, actor, op.source_batch_id)
            if state != op.command["rows"]:
                op.state = "stale"
                op.error_code = "undo_conflict"
                return
            result = changes.restore_snapshot(s, batch, rows, rules, actor)
            result.update(
                batch_id=op.source_batch_id,
                resumes_classification=op.command["summary"]["resumes_classification"],
            )
            if op.schema_version == 2:
                result["rules_restored"] = op.command["summary"]["rules_restore"]
            versions = [{"id": row.id, "version": row.version} for row in rows]
            op.undone_batch_id = op.source_batch_id
    except HTTPException as exc:
        # All shared validation precedes mutations. Stale records commit only
        # metadata, preserving the permanent operation ID and preventing retries.
        if exc.status_code not in (404, 409):
            raise
        op.state = "stale"
        op.error_code = (
            ("already_undone" if "already undone" in str(exc.detail) else "undo_conflict")
            if kind == "undo"
            else "stale_preview"
        )
        return
    except OperationError as exc:
        if exc.code not in {"undo_conflict", "batch_not_found"}:
            raise
        op.state = "stale"
        op.error_code = "undo_conflict"
        return
    op.state = "committed"
    op.committed_at = now()
    op.batch_id = result["batch_id"] if kind == "apply" else None
    op.receipt = bounded(
        {
            "operation_id": op.id,
            "kind": kind,
            "outcome": "no_change"
            if kind == "apply" and not result["changed"] and not result["future_rule"]
            else "applied"
            if kind == "apply"
            else "undone",
            **result,
            "versions": versions,
            "committed_at": op.committed_at.isoformat(),
        }
    )
    s.flush()  # audit, payment, job, receipt and unique undo guard share this commit


def get_operation(s, actor, operation_id):
    op = owned(s, actor, operation_id)
    code = op.error_code or {"stale": "stale_preview", "expired": "preview_expired"}.get(op.state)
    return {
        "operation_id": op.id,
        "kind": op.kind,
        "state": op.state,
        "error": code,
        "receipt": op.receipt,
        "current_revision": s.scalar(
            select(DashboardRevision.revision).where(DashboardRevision.id == 1)
        )
        or 0,
    }


def commit_operation(actor, operation_id, kind, *, session_factory=session, allow_batches=True):
    with session_factory() as s, s.begin():
        _commit(s, actor, operation_id, kind, allow_batches=allow_batches)
    # Revision triggers are deferred. This is a separately labelled observation,
    # not a fabricated revision inside the immutable receipt.
    with session_factory() as s:
        return get_operation(s, actor, operation_id)


def expire_previews(s):
    """Bound each worker pass; retain identities/receipts and skip active commits."""
    timeouts(s)
    timestamp = now()
    # Freeze each locked batch before UPDATE; a rescanned LIMIT subquery can
    # select more rows as the update changes its own eligibility predicate.
    expired = (
        select(CategoryOperation.id)
        .where(CategoryOperation.state == "prepared", CategoryOperation.expires_at <= timestamp)
        .order_by(CategoryOperation.expires_at, CategoryOperation.id)
        .limit(500)
        .with_for_update(skip_locked=True)
    )
    s.execute(
        update(CategoryOperation)
        .where(CategoryOperation.id.in_(list(s.scalars(expired))))
        .values(state="expired")
    )
    disposable = (
        select(CategoryOperation.id)
        .where(
            CategoryOperation.state.in_(["expired", "stale"]),
            CategoryOperation.created_at < timestamp - timedelta(hours=24),
            CategoryOperation.command.is_not(None),
        )
        .order_by(CategoryOperation.created_at, CategoryOperation.id)
        .limit(500)
        .with_for_update(skip_locked=True)
    )
    s.execute(
        update(CategoryOperation)
        .where(CategoryOperation.id.in_(list(s.scalars(disposable))))
        .values(command=None)
    )
