"""Gate 4 reports must use the host's restricted snapshot, never owner credentials."""

from datetime import UTC, datetime, timedelta

import pytest
from jsonschema import validate
from sqlalchemy import select, text
from test_mcp import CURSORS, seed

from monzo_backend import balances, budgets, monzo
from monzo_backend.db import engine, session
from monzo_backend.mcp.cursors import ToolFailure
from monzo_backend.mcp.server import TOOLS, execute
from monzo_backend.mcp.stored import BalanceResult, BudgetResult
from monzo_backend.models import Account, BalanceObservation, BalanceSnapshot, Connection, Job

INSTANT = datetime(2026, 10, 24, 14, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(balances, "now", lambda: INSTANT)
    monkeypatch.setattr(budgets, "now", lambda: INSTANT)


def run(name, arguments=None):
    data = execute(name, arguments or {}, engine(), CURSORS)
    validate(data, TOOLS[name][1].model_json_schema())
    return data


def observation(outcome="ok", age=0):
    data = {"amount_minor": 0, "currency": "GBP", "basis": "available"}
    with session() as s, s.begin():
        s.add(
            BalanceObservation(
                account_id="acc_test",
                source="current",
                attempted_at=INSTANT,
                retrieved_at=INSTANT - timedelta(minutes=age),
                outcome=outcome,
                data=data,
            )
        )
        s.add(
            BalanceSnapshot(
                account_id="acc_test",
                source="current",
                day=INSTANT.date(),
                attempted_at=INSTANT,
                retrieved_at=INSTANT - timedelta(minutes=age),
                data=data,
            )
        )


@pytest.mark.parametrize(
    "outcome,age,state",
    [
        ("ok", 0, "fresh"),
        ("ok", 46, "stale"),
        ("error", 0, "stale"),
        ("unsupported", 0, "unsupported"),
    ],
)
def test_balance_api_mcp_parity_and_unavailable_pots(client, owner_headers, outcome, age, state):
    observation(outcome, age)
    api = BalanceResult.model_validate(
        client.get("/v1/dashboard/balances", headers=owner_headers).json()
    ).model_dump(mode="json")
    result = run("get_balances")
    assert result == api
    assert result["items"][0]["state"] == state
    assert result["items"][0]["data"] == (
        None
        if state == "unsupported"
        else {"amount_minor": 0, "currency": "GBP", "basis": "available"}
    )
    assert result["items"][1]["state"] == "unavailable"
    assert result["items"][1]["data"] is None
    assert result["items"][0]["history"][0]["data"]["amount_minor"] == 0


def test_owner_currency_account_and_revision_boundaries():
    with session() as s, s.begin():
        s.add(Connection(id=2, user_id="other", access_token="fake", expires_at=INSTANT))
        s.flush()
        s.add_all(
            [
                Account(id="other", connection_id=2, kind="uk_retail"),
                Account(id="flex", connection_id=1, kind="uk_monzo_flex"),
                Account(id="rewards", connection_id=1, kind="uk_rewards"),
                Account(id="disabled", connection_id=1, kind="uk_retail", enabled=False),
            ]
        )
    for aid in ("other", "flex", "rewards", "disabled", "unknown"):
        with pytest.raises(ToolFailure, match="validation_error"):
            run("get_balances", {"account_id": aid})
    for aid in ("other", "rewards", "unknown"):
        with pytest.raises(ToolFailure, match="validation_error"):
            run("get_budget_status", {"period": "2026-10", "account_id": aid})
    assert {i["account_id"] for i in run("get_balances")["items"]} == {"acc_test"}
    assert (
        run("get_budget_status", {"period": "2026-10", "account_id": "flex"})["items"][0][
            "target_minor"
        ]
        is None
    )
    for name, args in (("get_balances", {}), ("get_budget_status", {"period": "2026-10"})):
        for invalid in (
            {"currency": "EUR"},
            {"expected_revision": -1},
            {"expected_revision": True},
            {"sql": "select secret"},
            {"amount_minor": 10},
        ):
            with pytest.raises(ToolFailure, match="validation_error"):
                run(name, {**args, **invalid})
        with pytest.raises(ToolFailure, match="data_changed"):
            run(name, {**args, "expected_revision": 999999})
        revision = run(name, args)["revision"]
        assert run(name, {**args, "expected_revision": revision})["revision"] == revision


@pytest.mark.parametrize("scope", ["combined", "acc_test"])
def test_budget_api_parity_versions_no_bank_or_owner_session(
    client, owner_headers, payment, monkeypatch, scope
):
    seed(payment)
    budgets.save(
        budgets.BudgetEdit(
            period="2026-10", account_id=scope, amount_minor=1000, expected_version=0
        )
    )
    api = BudgetResult.model_validate(
        client.get(
            "/v1/dashboard/budgets",
            params={"period": "2026-10", "account_id": scope},
            headers=owner_headers,
        ).json()
    ).model_dump(mode="json")
    with session() as s:
        jobs = list(s.scalars(select(Job.id)))

    def forbidden(*args, **kwargs):
        raise AssertionError("Report attempted a bank fetch or owner database session")

    monkeypatch.setattr(budgets, "session", forbidden)
    monkeypatch.setattr(balances, "session", forbidden)
    monkeypatch.setattr(monzo, "bank_get", forbidden)
    monkeypatch.setattr(balances, "schedule", forbidden)
    result = run("get_budget_status", {"period": "2026-10", "account_id": scope})
    assert result == api
    assert result["items"][0]["remaining_minor"] == -234
    assert result["items"][0]["version"] == 1
    assert result["coverage"]["state"] == "incomplete"
    assert run("get_balances")["items"][0]["state"] == "unavailable"
    with session() as s:
        assert list(s.scalars(select(Job.id))) == jobs


def test_unset_future_budgets_are_not_fabricated_allowances():
    result = run("get_budget_status", {"period": "2027-01"})
    assert result["coverage"]["state"] == "not_started"
    assert all(
        i["target_minor"] is None and i["remaining_minor"] is None and i["version"] == 0
        for i in result["items"]
    )
    for period in ("2026-13", "2026-1", "1999-01"):
        with pytest.raises(ToolFailure, match="validation_error"):
            run("get_budget_status", {"period": period})


def test_balance_snapshot_revision_is_pinned_while_later_writes_commit():
    from monzo_backend.mcp.database import read_snapshot
    from monzo_backend.mcp.stored import BalanceInput, StoredReports

    observation()
    with read_snapshot(engine()) as s:
        reports = StoredReports(s)
        first = reports.balances(BalanceInput())
        with session() as writer, writer.begin():
            writer.get(BalanceObservation, ("acc_test", "current")).data = {
                "amount_minor": -1234,
                "currency": "GBP",
                "basis": "available",
            }
        second = reports.balances(BalanceInput(expected_revision=first["revision"]))
        assert first == second
    assert run("get_balances")["items"][0]["data"]["amount_minor"] == -1234


def test_new_tables_have_only_explicit_select_grants():
    from monzo_backend.mcp.database import READ_TABLES, reporting_engine

    observation()
    budgets.save(budgets.BudgetEdit(period="2026-10", amount_minor=5000, expected_version=0))
    url = engine().url.set(username="monzo_mcp", password="isolated-test-only")
    with engine().begin() as conn:
        conn.execute(text("CREATE ROLE monzo_mcp LOGIN NOINHERIT PASSWORD 'isolated-test-only'"))
        conn.execute(text("GRANT CONNECT ON DATABASE monzo_test TO monzo_mcp"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO monzo_mcp"))
        conn.execute(text("GRANT SELECT ON " + ",".join(READ_TABLES) + " TO monzo_mcp"))
    ro = None
    try:
        ro = reporting_engine(url.render_as_string(hide_password=False))
        assert execute("get_balances", {}, ro, CURSORS)["items"][0]["data"]["amount_minor"] == 0
        assert (
            execute("get_budget_status", {"period": "2026-10"}, ro, CURSORS)["items"][0][
                "target_minor"
            ]
            == 5000
        )
        # Turn off read-only transaction mode to prove the SQL grants also enforce the boundary.
        for query in (
            "DELETE FROM budget_versions",
            "UPDATE balance_observations SET outcome='ok'",
            "INSERT INTO balance_snapshots(account_id) VALUES ('bad')",
            "SELECT access_token FROM bank_connections",
            "SELECT token_hash FROM browser_sessions",
            "SELECT payload FROM jobs",
        ):
            with pytest.raises(Exception, match="permission denied"):
                with ro.begin() as conn:
                    conn.execute(text("SET TRANSACTION READ WRITE"))
                    conn.execute(text(query))
        with engine().begin() as conn:
            conn.execute(text("GRANT UPDATE ON budget_versions TO monzo_mcp"))
        with pytest.raises(ValueError, match="allowlist"):
            reporting_engine(url.render_as_string(hide_password=False))
    finally:
        if ro:
            ro.dispose()
        with engine().begin() as conn:
            conn.execute(text("DROP OWNED BY monzo_mcp"))
            conn.execute(text("DROP ROLE monzo_mcp"))
