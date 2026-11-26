"""Gate F: bounded frozen merchant batches and guarded future-rule undo."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_category_operations import ACTOR, commit, state, undo_preview

from monzo_backend import category_changes as changes
from monzo_backend import category_operations as ops
from monzo_backend import merchant_groups
from monzo_backend.db import session
from monzo_backend.domain import ingest
from monzo_backend.models import (
    Account,
    Audit,
    CategoryOperation,
    CategoryWriteControl,
    Rule,
    Transaction,
    now,
)


@pytest.fixture
def ready(payment):
    with session() as s, s.begin():
        s.add(CategoryWriteControl(id=1, enabled=True))
        tx = ingest(s, "acc_test", payment)
        tx.category, tx.category_source, tx.category_status = "shopping", "ai", "needs_review"
    return payment


def batch(scope="unverified", future_rule=False, group=False, version=1, **extra):
    with session() as s, s.begin():
        g = merchant_groups.for_merchant(s, "merch_test")
        return ops.prepare_batch(
            s,
            ACTOR,
            "tx_test",
            extra.get("merchant_id", "merch_test"),
            extra.get("merchant_group_id", g.id if group else None),
            changes.CategoryChoice(
                category="transport",
                expected_version=version,
                scope=scope,
                future_rule=future_rule,
                group_merchant=group and scope != "this",
            ),
        )


def add(payment, ident, *, verified=False, merchant="merch_test"):
    with session() as s, s.begin():
        tx = ingest(
            s,
            "acc_test",
            {**payment, "id": ident, "merchant": {"id": merchant, "name": "Test Shop"}},
        )
        tx.category, tx.category_source, tx.category_status = (
            "bills",
            "manual" if verified else "ai",
            "assigned" if verified else "needs_review",
        )


def test_unverified_default_frozen_ids_counts_and_undo(ready):
    add(ready, "tx_other")
    add(ready, "tx_protected", verified=True)
    p = batch()
    assert p["counts"]["matched_count"] == 3
    assert p["counts"]["protected_count"] == 1
    assert p["counts"]["change_count"] == 2
    assert p["account_ids"] == ["acc_test"] and p["later_arrivals_excluded"]
    assert len(p["payments"]) == 3 and not p["future_rule"]
    add(ready, "tx_new_arrival")
    r = commit(p)
    assert r["receipt"]["changed"] == 2 and len(r["receipt"]["versions"]) == 2
    assert commit(p)["receipt"] == r["receipt"]
    with session() as s:
        assert s.get(Transaction, "tx_new_arrival").category == "bills"
        assert s.get(Transaction, "tx_protected").version == 1
    u = undo_preview(r["receipt"]["batch_id"])
    assert len(u["payments"]) == 2
    assert commit(u, "undo")["receipt"]["restored"] == 2
    assert state() == ("shopping", "ai", "needs_review", 3)


@pytest.mark.parametrize("count", [100, 101])
@pytest.mark.parametrize("protected", [False, True])
def test_boundary_includes_protected_and_unchanged_candidates(ready, count, protected):
    with session() as s, s.begin():
        for i in range(count - 1):
            tx = ingest(s, "acc_test", {**ready, "id": f"tx_{i:03}"})
            tx.category, tx.category_source, tx.category_status = (
                "transport",
                "manual" if protected else "ai",
                "assigned" if protected else "needs_review",
            )
    if count == 101:
        with pytest.raises(ops.OperationError, match="batch_too_large"):
            batch()
        with session() as s:
            assert s.scalar(select(func.count()).select_from(CategoryOperation)) == 0
    else:
        p = batch()
        assert len(p["payments"]) == 100
        assert commit(p)["receipt"]["changed"] == (1 if protected else 100)


def test_exact_identity_and_group_all_rejection(ready):
    with pytest.raises(ops.OperationError, match="merchant_mismatch"):
        batch(merchant_id="lookalike_name")
    with pytest.raises(ops.OperationError, match="group_mismatch"):
        batch(merchant_group_id="wrong_group")
    with pytest.raises(ops.OperationError, match="group_mismatch"):
        batch(future_rule=True)
    with pytest.raises(changes.HTTPException):
        batch(scope="all", group=True)


def test_all_requires_explicit_scope_and_reports_verified_replacement(ready):
    add(ready, "tx_verified", verified=True)
    p = batch(scope="all")
    assert p["counts"]["verified_replaced_count"] == 1
    assert commit(p)["receipt"]["changed"] == 2


def test_group_batch_future_rule_and_restore_existing_rule(ready):
    add(ready, "tx_group", merchant="merch_other")
    with session() as s, s.begin():
        g = merchant_groups.for_merchant(s, "merch_test")
        s.add(Rule(group_id=g.id, category="health", effective_from=now() - timedelta(days=2)))
    p = batch(group=True, future_rule=True)
    assert p["counts"]["change_count"] == 2
    assert p["rule_target"] == "group:" + p["merchant_group_id"]
    old = p["rules_before"][p["rule_target"]]
    r = commit(p)
    with session() as s:
        rule = s.scalar(select(Rule))
        assert (
            rule.category == "transport"
            and rule.effective_from.isoformat() != old["effective_from"]
        )
    u = undo_preview(r["receipt"]["batch_id"])
    assert u["rules_restore"][p["rule_target"]] == old
    commit(u, "undo")
    with session() as s:
        assert changes.rule_state(s.scalar(select(Rule))) == old


def test_rule_only_commit_is_applied_and_undo_deletes_new_rule(ready):
    with session() as s, s.begin():
        tx = s.get(Transaction, "tx_test")
        tx.category, tx.category_source, tx.category_status = "transport", "manual", "assigned"
    p = batch(group=True, future_rule=True)
    assert p["counts"]["change_count"] == 0
    r = commit(p)
    assert r["receipt"]["outcome"] == "applied" and r["receipt"]["changed"] == 0
    u = undo_preview(r["receipt"]["batch_id"])
    assert u["payments"] == [] and u["future_rule"]
    assert commit(u, "undo")["receipt"]["restored"] == 0
    with session() as s:
        assert s.scalar(select(Rule)) is None


@pytest.mark.parametrize("change", ["group", "rule", "worker", "settlement", "account"])
def test_batch_stale_state_never_partially_changes_rows_or_rule(ready, change):
    add(ready, "tx_other")
    p = batch(group=True, future_rule=True)
    with session() as s, s.begin():
        if change == "group":
            merchant_groups.for_merchant(s, "merch_test").version += 1
        elif change == "rule":
            s.add(Rule(merchant_id="merch_test", category="health"))
        else:
            tx = s.get(Transaction, "tx_other")
            if change == "worker":
                tx.category = "health"
            elif change == "settlement":
                tx.settled_at = now()
            else:
                s.get(Account, "acc_test").kind = "uk_rewards"
    assert commit(p)["state"] == "stale"
    assert state()[0] == "shopping"
    with session() as s:
        assert s.scalar(select(Audit).where(Audit.action == "category_batch_applied")) is None


@pytest.mark.parametrize("change", ["rule", "group", "row"])
def test_undo_rejects_later_group_rule_or_payment_change(ready, change):
    r = commit(batch(group=True, future_rule=True))
    u = undo_preview(r["receipt"]["batch_id"])
    with session() as s, s.begin():
        if change == "rule":
            s.scalar(select(Rule)).category = "bills"
        elif change == "group":
            merchant_groups.for_merchant(s, "merch_test").version += 1
        else:
            s.get(Transaction, "tx_test").version += 1
    assert commit(u, "undo")["state"] == "stale"
    assert state()[0] == "transport"


def test_batch_concurrency_and_atomic_fault(ready, monkeypatch):
    add(ready, "tx_other")
    p = batch(group=True, future_rule=True)
    original = changes.apply_snapshot

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        args[0].flush()
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(changes, "apply_snapshot", fail)
    with pytest.raises(RuntimeError):
        commit(p)
    with session() as s:
        assert s.scalar(select(Rule)) is None
        assert s.get(CategoryOperation, p["operation_id"]).state == "prepared"
    monkeypatch.setattr(changes, "apply_snapshot", original)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(commit, [p, p]))
    assert results[0]["receipt"] == results[1]["receipt"]
    assert results[0]["receipt"]["changed"] == 2


def test_schema2_switch_and_old_schema_rollback_contract(ready):
    p = batch()
    with session() as s:
        assert s.get(CategoryOperation, p["operation_id"]).schema_version == 2
    with pytest.raises(ops.OperationError, match="batches_disabled"):
        ops.commit_operation(ACTOR, p["operation_id"], "apply", allow_batches=False)
    r = commit(p)
    assert (
        ops.commit_operation(ACTOR, p["operation_id"], "apply", allow_batches=False)["receipt"]
        == r["receipt"]
    )
    with session() as s, s.begin(), pytest.raises(ops.OperationError, match="batches_disabled"):
        ops.prepare_undo(s, ACTOR, r["receipt"]["batch_id"], allow_batches=False)


@pytest.mark.parametrize("stage", ["preview", "undo"])
def test_real_group_merge_fences_batch_and_undo(ready, stage):
    p = batch(group=True, future_rule=True)
    if stage == "undo":
        p = undo_preview(commit(p)["receipt"]["batch_id"])
    with session() as s, s.begin():
        ingest(
            s,
            "acc_test",
            {
                **ready,
                "id": "tx_other_group",
                "merchant": {"id": "other_merch", "name": "Different group"},
            },
        )
        source = merchant_groups.for_merchant(s, "merch_test")
        target = merchant_groups.for_merchant(s, "other_merch")
        merchant_groups.merge(s, target.id, source.id, target.version, source.version, "source")
    assert commit(p, "apply" if stage == "preview" else "undo")["state"] == "stale"


def test_pending_batch_undo_discloses_jobs_and_future_rule_cutoff(ready):
    from monzo_backend.domain import deterministic
    from monzo_backend.models import Job

    with session() as s, s.begin():
        tx = s.get(Transaction, "tx_test")
        tx.category, tx.category_source, tx.category_status = None, None, "pending"
    r = commit(batch(group=True, future_rule=True))
    assert r["receipt"]["rules_after"] and r["receipt"]["rules_before"]
    with session() as s, s.begin():
        rule = s.scalar(select(Rule))
        tx = ingest(
            s,
            "acc_test",
            {
                **ready,
                "id": "tx_later",
                "created": (rule.effective_from + timedelta(seconds=1)).isoformat(),
            },
        )
        assert deterministic(s, tx)["category"] == "transport"
        tx.created_at = rule.effective_from - timedelta(seconds=1)
        assert deterministic(s, tx) is None or deterministic(s, tx).get("source") != "rule"
    u = undo_preview(r["receipt"]["batch_id"])
    assert u["resumes_classification"]
    commit(u, "undo")
    with session() as s:
        assert s.scalar(select(Job).where(Job.dedupe_key == "category:tx_test:3:undo")) is not None
