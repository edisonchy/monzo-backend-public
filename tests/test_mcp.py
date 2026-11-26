import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jsonschema import validate
from pydantic import ValidationError
from sqlalchemy import select, text
from starlette.testclient import TestClient

from monzo_backend.db import engine, session
from monzo_backend.domain import ingest
from monzo_backend.mcp.cursors import Cursors, ToolFailure
from monzo_backend.mcp.database import read_snapshot
from monzo_backend.mcp.security import AuthFailure, MCPSettings, TokenVerifier
from monzo_backend.mcp.server import TOOLS, create_app, execute
from monzo_backend.models import Account, Audit, Job, ReportingCoverage, Transaction
from monzo_backend.monzo import execute as sync_execute

SCOPE = {"date_from": "2026-10-01", "date_to": "2026-10-16"}
CURSORS = Cursors("test-cursor-secret-" + "a" * 32, "issuer|owner")


def tool(name, arguments=None):
    result = execute(name, arguments or SCOPE, engine(), CURSORS)
    validate(result, TOOLS[name][1].model_json_schema())
    return result


def seed(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", {**payment, "settled": "2026-10-16T01:00:00Z"})
        tx = s.get(Transaction, payment["id"])
        tx.category = "shopping"
        tx.category_status = "needs_review"
        tx.category_source = "ai"
        tx.reporting_facts = {"scheme": "mastercard"}


def coverage():
    with session() as s, s.begin():
        s.add(
            ReportingCoverage(
                account_id="acc_test",
                start_at=datetime(2026, 9, 30, tzinfo=UTC),
                end_at=datetime(2026, 10, 17, tzinfo=UTC),
                source="test_fixture",
            )
        )


def test_summary_matches_dashboard_and_preserves_writes(payment, client, owner_headers):
    seed(payment)
    with session() as s, s.begin():
        ingest(
            s,
            "acc_test",
            {**payment, "id": "refund", "amount": 500, "settled": "2026-10-16T01:00:00Z"},
        )
        ingest(s, "acc_test", {**payment, "id": "pending", "amount": -888})
        ingest(
            s, "acc_test", {**payment, "id": "declined", "amount": -999, "decline_reason": "OTHER"}
        )
        ingest(
            s,
            "acc_test",
            {
                **payment,
                "id": "euro",
                "currency": "EUR",
                "amount": -100,
                "settled": "2026-10-16T01:00:00Z",
            },
        )
        before = [
            (t.id, t.version, t.category, t.category_status)
            for t in s.scalars(select(Transaction).order_by(Transaction.id))
        ]
        jobs = list(s.scalars(select(Job.id)))
        audits = list(s.scalars(select(Audit.id)))
    result = tool("get_summary")
    totals = result["totals"]
    assert totals["spending_minor"] == 1234
    assert totals["money_in_minor"] == 500
    assert totals["pending_outgoing_minor"] == 888
    assert totals["declined_count"] == 1
    assert totals["mean_outgoing_minor"] == "1234.00"
    report = client.get(
        "/v1/dashboard/report",
        params={"account_id": "combined", "period": "2026-10"},
        headers=owner_headers,
    ).json()["current"]
    assert totals["spending_minor"] == report["debits_minor"]
    assert totals["money_in_minor"] == report["credits_minor"]
    with session() as s:
        assert [
            (t.id, t.version, t.category, t.category_status)
            for t in s.scalars(select(Transaction).order_by(Transaction.id))
        ] == before
        assert list(s.scalars(select(Job.id))) == jobs
        assert list(s.scalars(select(Audit.id))) == audits


def test_combined_exclusion_requires_bank_evidence(payment):
    seed(payment)
    with session() as s, s.begin():
        tx = s.get(Transaction, payment["id"])
        tx.reporting_facts = {
            "scheme": "uk_retail_pot",
            "pot_ref": "pot",
            "pot_account_ref": "account",
        }
    assert tool("get_summary")["totals"]["spending_minor"] == 0
    assert tool("get_summary")["totals"]["excluded_outgoing_minor"] == 1234
    assert (
        tool("get_summary", {**SCOPE, "account_ids": ["acc_test"]})["totals"]["spending_minor"]
        == 1234
    )
    assert (
        tool("get_transaction", {"transaction_id": payment["id"]})["payment"]["exclusion_reason"]
        == "pot_transfer"
    )


def test_breakdown_uses_persistent_group_and_full_denominator(payment):
    seed(payment)
    seed(
        {
            **payment,
            "id": "other_branch",
            "merchant": {"id": "another_bank_id", "name": "Test Shop"},
            "amount": -200,
        }
    )
    seed(
        {
            **payment,
            "id": "different",
            "merchant": {"id": "other_merchant", "name": "Other Shop"},
            "amount": -100,
        }
    )
    result = tool("get_breakdown", {**SCOPE, "dimensions": ["merchant_group"], "limit": 1})
    assert result["matching_groups"] == 2
    assert result["rows"][0]["value"] == 1434
    assert result["denominator"] == 1534
    assert result["rows"][0]["keys"][0].startswith("group:")
    second = tool(
        "get_breakdown",
        {**SCOPE, "dimensions": ["merchant_group"], "limit": 1, "cursor": result["next_cursor"]},
    )
    assert second["rows"][0]["value"] == 100
    assert second["next_cursor"] is None


def test_pagination_ties_tamper_filters_and_changed_revision(payment):
    seed(payment)
    seed({**payment, "id": "tx_second"})
    first = tool("search_transactions", {**SCOPE, "limit": 1})
    second = tool("search_transactions", {**SCOPE, "limit": 1, "cursor": first["next_cursor"]})
    assert first["rows"][0]["id"] != second["rows"][0]["id"]
    assert first["matching_count"] == second["matching_count"] == 2
    assert first["rows"][0]["description"] is None
    for token, filters in [
        (first["next_cursor"][:-3] + "xxx", SCOPE),
        (first["next_cursor"], {**SCOPE, "direction": "out"}),
    ]:
        with pytest.raises(ToolFailure, match="validation_error"):
            tool("search_transactions", {**filters, "cursor": token})
    with session() as s, s.begin():
        s.get(Transaction, payment["id"]).amount -= 1
    with pytest.raises(ToolFailure, match="data_changed"):
        tool("search_transactions", {**SCOPE, "cursor": first["next_cursor"]})


def test_expired_cursor(monkeypatch):
    token = CURSORS.encode("f", 1, ["a"])
    monkeypatch.setattr("monzo_backend.mcp.cursors.time.time", lambda: time.time_ns() / 1e9 + 901)
    with pytest.raises(ToolFailure, match="validation_error"):
        CURSORS.decode(token, "f", 1)


def test_unknown_history_is_not_zero_filled(payment):
    seed(payment)
    result = tool("get_time_series")
    assert len(result["buckets"]) == 1
    assert result["metadata"]["complete"] is False
    assert result["buckets"][0]["coverage_complete"] is False
    assert tool("get_time_series", {**SCOPE, "profile": "weekday"})["profile"] == []
    coverage()
    result = tool("get_time_series")
    assert len(result["buckets"]) == 16
    assert result["metadata"]["complete"] is True
    assert sum(b["spending_minor"] for b in result["buckets"]) == 1234
    profile = tool("get_time_series", {**SCOPE, "profile": "day_of_month"})["profile"]
    assert profile[0] == {"key": 1, "observed_days": 1, "mean_spending_minor": "0.00"}


def test_comparison_same_snapshot_zero_baseline(payment):
    seed(payment)
    result = tool(
        "compare_periods",
        {
            "selected": SCOPE,
            "previous": {"date_from": "2026-09-01", "date_to": "2026-09-16"},
            "attribution": "merchant_group",
        },
    )
    assert result["difference_minor"] == 1234
    assert result["change_percent"] is None
    assert (
        result["selected"]["metadata"]["data_revision"]
        == result["previous"]["metadata"]["data_revision"]
    )
    assert result["contributors"][0]["difference_minor"] == 1234


def test_detail_protects_other_accounts_and_exposes_description_only_explicitly(payment):
    seed(payment)
    assert (
        tool("get_transaction", {"transaction_id": payment["id"]})["payment"]["description"]
        == payment["description"]
    )
    with session() as s, s.begin():
        s.add(Account(id="unsupported", connection_id=1, kind="uk_rewards"))
        s.flush()
        ingest(s, "unsupported", {**payment, "id": "tx_rewards", "account_id": "unsupported"})
    with pytest.raises(ToolFailure, match="validation_error"):
        tool("get_transaction", {"transaction_id": "tx_rewards"})
    with pytest.raises(ToolFailure, match="validation_error"):
        tool("get_summary", {**SCOPE, "account_ids": ["unsupported"]})


def test_quality_does_not_promote_ai_suggestions(payment):
    seed(payment)
    result = tool("get_category_quality")
    assert result["rows"][0]["suggested_count"] == 1
    assert result["rows"][0]["suggested_categories"] == ["shopping"]
    assert not result["rows"][0]["conflicting_suggestions"]
    context = tool("get_data_context", {"expected_revision": result["metadata"]["data_revision"]})
    assert context["capabilities"] == list(TOOLS)
    assert "balance" not in json.dumps(context["accounts"])


@pytest.mark.parametrize(
    "extra",
    [
        {"date_to": "2100-01-01"},
        {"date_to": "2026-09-01"},
        {"categories": ["fake"]},
        {"limit": 101},
        {"sql": "SELECT * FROM bank_connections"},
        {"min_amount_minor": 5, "max_amount_minor": 1},
        {"limit": True},
    ],
)
def test_invalid_inputs_rejected(extra):
    with pytest.raises(ToolFailure, match="validation_error"):
        tool("search_transactions", {**SCOPE, **extra})


def test_snapshot_is_database_read_only():
    with pytest.raises(Exception, match="read-only"):
        with read_snapshot(engine()) as s:
            s.execute(text("UPDATE accounts SET history_status='changed'"))


def test_coverage_only_after_final_page(monkeypatch, payment):
    from monzo_backend.models import now

    end = now()
    payload = {
        "account_id": "acc_test",
        "since": (end - timedelta(days=7)).isoformat(),
        "before": end.isoformat(),
    }
    monkeypatch.setattr(
        "monzo_backend.monzo.bank_get",
        lambda *_: {"transactions": [{**payment, "id": f"tx_{i}"} for i in range(100)]},
    )
    sync_execute({"id": "full", "kind": "sync_page", "payload": payload})
    with session() as s:
        assert s.scalar(select(ReportingCoverage)) is None
        follow = s.scalar(select(Job).where(Job.queue == "sync")).payload
    monkeypatch.setattr("monzo_backend.monzo.bank_get", lambda *_: {"transactions": []})
    sync_execute({"id": "final", "kind": "sync_page", "payload": follow})
    with session() as s:
        row = s.scalar(select(ReportingCoverage))
        assert row.start_at == datetime.fromisoformat(payload["since"]) + timedelta(microseconds=1)
        assert row.end_at == end


def auth_settings(tmp_path):
    enable = tmp_path / "access"
    enable.write_text("enabled\n")
    return MCPSettings(
        enabled=True,
        issuer="https://issuer.example",
        jwks_url="https://issuer.example/jwks",
        owner_subject="owner",
        cursor_secret="a" * 40,
        database_url="postgresql://monzo_mcp:unused@unused/unused",
        enable_file=enable,
    )


def token(key, settings, **overrides):
    current = int(time.time())
    claims = {
        "iss": settings.issuer,
        "aud": settings.resource,
        "sub": "owner",
        "scope": "finance:read",
        "iat": current,
        "exp": current + 300,
        **overrides,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.parametrize(
    "overrides,status",
    [
        ({}, 200),
        ({"sub": "someone_else"}, 403),
        ({"scope": "other"}, 403),
        ({"aud": "browser"}, 401),
        ({"iss": "https://evil.example"}, 401),
        ({"exp": 1}, 401),
        ({"exp": int(time.time()) + 3600}, 401),
    ],
)
def test_jwt_owner_scope_audience_lifetime(tmp_path, respx_mock, overrides, status, caplog):
    settings = auth_settings(tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    respx_mock.get(settings.jwks_url).mock(
        return_value=httpx.Response(200, json={"keys": [{**jwk, "kid": "test-key"}]})
    )
    verifier = TokenVerifier(settings)

    async def check():
        if status == 200:
            assert (await verifier.verify("Bearer " + token(key, settings, **overrides)))[
                "sub"
            ] == "owner"
        else:
            with pytest.raises(AuthFailure) as error:
                await verifier.verify("Bearer " + token(key, settings, **overrides))
            assert error.value.status == status

    asyncio.run(check())
    for record in caplog.records:
        if record.name == "monzo.mcp.auth":
            assert record.getMessage().startswith("mcp_auth_rejected reason=")
            assert record.args[0] in {
                "InvalidAudienceError",
                "InvalidIssuerError",
                "ExpiredSignatureError",
                "ValueError",
            }


def test_http_auth_protocol_structured_output_and_disable(tmp_path, payment, respx_mock, capfd):
    seed(payment)
    settings = auth_settings(tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    respx_mock.get(settings.jwks_url).mock(
        return_value=httpx.Response(200, json={"keys": [{**jwk, "kid": "test-key"}]})
    )
    app = create_app(settings, engine=engine())
    headers = {
        "Authorization": "Bearer " + token(key, settings),
        "Accept": "application/json, text/event-stream",
    }

    def rpc(client, method, params=None):
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers=headers,
        )

    with TestClient(app, base_url="https://spending.example.invalid") as client:
        assert client.post("/mcp").status_code == 401
        assert "resource_metadata=" in client.post("/mcp").headers["www-authenticate"]
        meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == settings.resource
        init = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert init.status_code == 200
        listing = rpc(client, "tools/list").json()["result"]["tools"]
        assert len(listing) == len(TOOLS)
        assert all(t["annotations"]["readOnlyHint"] for t in listing)
        assert all(t["inputSchema"]["additionalProperties"] is False for t in listing)
        result = rpc(client, "tools/call", {"name": "get_summary", "arguments": SCOPE})
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["result"]["structuredContent"]["totals"]["spending_minor"] == 1234
        error = rpc(
            client, "tools/call", {"name": "get_summary", "arguments": {"sql": "private"}}
        ).json()["result"]
        assert error["isError"] is True
        assert error["structuredContent"]["error"]["code"] == "validation_error"
        assert "private" not in json.dumps(error)
        # Same already-issued token is denied on the next request after local disable.
        settings.enable_file.write_text("disabled\n")
        assert rpc(client, "tools/list").status_code == 503

    audit = capfd.readouterr().err
    assert "tool=get_summary outcome=ok" in audit
    assert "tool=get_summary outcome=validation_error" in audit
    assert "private" not in audit
    assert payment["description"] not in audit
    assert headers["Authorization"] not in audit


def test_audit_logging_works_without_root_info_and_is_not_duplicated():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
from monzo_backend.mcp.server import create_app
create_app()
create_app()
logging.getLogger('monzo.mcp').info('mcp_call sanitized_test')
logging.getLogger('httpx').info('must_not_enable_dependency_logging')
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr.count("mcp_call sanitized_test") == 1
    assert "must_not_enable_dependency_logging" not in result.stderr


def test_startup_settings_fail_closed(tmp_path):
    with pytest.raises(ValidationError):
        MCPSettings(enabled=True)
    settings = auth_settings(tmp_path)
    settings.enable_file.unlink()
    assert not settings.access_enabled()


def test_real_reporting_role_denies_credentials_and_writes(payment):
    from monzo_backend.mcp.database import READ_TABLES, reporting_engine

    seed(payment)
    url = engine().url.set(username="monzo_mcp", password="isolated-test-only")
    with engine().begin() as conn:
        conn.execute(text("CREATE ROLE monzo_mcp LOGIN NOINHERIT PASSWORD 'isolated-test-only'"))
        conn.execute(text("GRANT CONNECT ON DATABASE monzo_test TO monzo_mcp"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO monzo_mcp"))
        conn.execute(text("GRANT SELECT ON " + ",".join(READ_TABLES) + " TO monzo_mcp"))
    ro = None
    try:
        ro = reporting_engine(url.render_as_string(hide_password=False))
        result = execute("get_summary", SCOPE, ro, CURSORS)
        assert result["totals"]["spending_minor"] == 1234
        for query in [
            "SELECT access_token FROM bank_connections",
            "SELECT token_hash FROM browser_sessions",
            "SELECT command FROM category_operations",
            "SELECT enabled FROM category_write_control",
            "SELECT attempts FROM category_rate_limits",
            "UPDATE transactions SET amount=0",
            "INSERT INTO jobs(id) VALUES ('bad')",
        ]:
            with pytest.raises(Exception, match="permission denied|read-only"):
                with ro.begin() as conn:
                    conn.execute(text(query))
        with engine().begin() as conn:
            conn.execute(text("GRANT SELECT(access_token) ON bank_connections TO monzo_mcp"))
        with pytest.raises(ValueError, match="allowlist"):
            reporting_engine(url.render_as_string(hide_password=False))
    finally:
        if ro:
            ro.dispose()
        with engine().begin() as conn:
            conn.execute(text("DROP OWNED BY monzo_mcp"))
            conn.execute(text("DROP ROLE monzo_mcp"))


def test_recurring_requires_coverage_and_regular_series(payment):
    scope = {"date_from": "2026-01-01", "date_to": "2026-06-30"}
    with pytest.raises(ToolFailure, match="insufficient_data"):
        tool("get_recurring_payments", scope)
    with session() as s, s.begin():
        s.add(
            ReportingCoverage(
                account_id="acc_test",
                start_at=datetime(2025, 12, 31, tzinfo=UTC),
                end_at=datetime(2026, 7, 1, tzinfo=UTC),
                source="test_fixture",
            )
        )
    for i, day in enumerate(["2026-02-15", "2026-03-15", "2026-04-15"]):
        seed({**payment, "id": f"monthly_{i}", "created": f"{day}T12:00:00Z"})
    result = tool("get_recurring_payments", scope)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["cadence"] == "monthly"
    assert result["findings"][0]["occurrence_count"] == 3
    # An irregular fourth purchase invalidates the simple same-amount cadence.
    seed({**payment, "id": "irregular", "created": "2026-04-17T12:00:00Z"})
    assert tool("get_recurring_payments", scope)["findings"] == []


def test_unusual_activity_zero_mad_new_merchant_and_duplicate(payment):
    with session() as s, s.begin():
        s.add(
            ReportingCoverage(
                account_id="acc_test",
                start_at=datetime(2025, 12, 31, tzinfo=UTC),
                end_at=datetime(2026, 5, 1, tzinfo=UTC),
                source="test_fixture",
            )
        )
    for i in range(10):
        seed(
            {
                **payment,
                "id": f"past_{i}",
                "amount": -1000,
                "created": f"2026-01-{i + 1:02}T12:00:00Z",
            }
        )
    seed({**payment, "id": "large", "amount": -5000, "created": "2026-04-01T12:00:00Z"})
    seed({**payment, "id": "duplicate", "amount": -5000, "created": "2026-04-01T13:00:00Z"})
    seed(
        {
            **payment,
            "id": "new",
            "amount": -100,
            "created": "2026-04-02T12:00:00Z",
            "merchant": {"id": "new", "name": "New"},
        }
    )
    result = tool(
        "find_unusual_activity",
        {
            "date_from": "2026-04-01",
            "date_to": "2026-04-30",
            "baseline_from": "2026-01-01",
            "baseline_to": "2026-03-31",
        },
    )
    kinds = [row["kind"] for row in result["findings"]]
    assert kinds.count("large_payment") == 2
    assert kinds.count("spending_spike") == 1
    assert kinds.count("possible_duplicate") == 1
    assert kinds.count("new_merchant") == 1
    assert next(row for row in result["findings"] if row["kind"] == "new_merchant")[
        "evidence_ids"
    ] == ["new"]


def test_jwks_unavailable_does_not_use_unverified_claims(tmp_path, respx_mock):
    settings = auth_settings(tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    route = respx_mock.get(settings.jwks_url).mock(return_value=httpx.Response(503))
    verifier = TokenVerifier(settings)

    async def check():
        for _ in range(2):
            with pytest.raises(AuthFailure):
                await verifier.verify("Bearer " + token(key, settings))

    asyncio.run(check())
    assert route.call_count == 1


def test_cumulative_series_carries_unknown_coverage_forward(payment):
    seed(payment)
    with session() as s, s.begin():
        s.add(
            ReportingCoverage(
                account_id="acc_test",
                start_at=datetime(2026, 10, 14, tzinfo=UTC),
                end_at=datetime(2026, 10, 17, tzinfo=UTC),
                source="test_fixture",
            )
        )
    result = tool("get_time_series", {**SCOPE, "cumulative": True})
    assert result["buckets"][-1]["spending_minor"] == 1234
    assert result["buckets"][-1]["coverage_complete"] is False


def test_category_quality_includes_legacy_rules_in_group(payment):
    from monzo_backend.models import Rule

    seed(payment)
    with session() as s, s.begin():
        s.add(Rule(merchant_id="merch_test", category="groceries"))
    result = tool("get_category_quality")
    assert result["rows"][0]["rules"][0]["merchant_id"] == "merch_test"
    assert result["rows"][0]["rules"][0]["category"] == "groceries"


def test_london_calendar_day_handles_dst(payment):
    # The October change gives 25 hours to the 25th; use a past year to avoid future inputs.
    seed({**payment, "created": "2025-10-25T23:30:00Z", "id": "before_clock_change"})
    seed({**payment, "created": "2025-10-26T23:30:00Z", "id": "after_clock_change"})
    result = tool("get_summary", {"date_from": "2025-10-26", "date_to": "2025-10-26"})
    assert result["totals"]["count"] == 2


def test_empty_summary_has_no_mean_or_invented_coverage():
    result = tool("get_summary")
    assert result["totals"]["mean_outgoing_minor"] is None
    assert result["metadata"]["complete"] is False


class AcceptedTestToken:
    async def verify(self, _header):
        return {"sub": "owner", "scope": "finance:read", "exp": int(time.time()) + 300}


def test_transport_rate_limit_and_response_size(tmp_path, payment):
    seed(payment)
    with session() as s, s.begin():
        s.get(Transaction, payment["id"]).description = "x" * 200000
    settings = auth_settings(tmp_path)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer test-fixture",
    }
    with TestClient(
        create_app(settings, engine=engine(), verifier=AcceptedTestToken()),
        base_url="https://spending.example.invalid",
    ) as client:
        result = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_transaction",
                    "arguments": {"transaction_id": payment["id"]},
                },
            },
        )
        assert result.json()["result"]["structuredContent"]["error"]["code"] == "validation_error"
        assert len(result.content) < 256 * 1024
        statuses = [client.get("/mcp", headers=headers).status_code for _ in range(20)]
        assert 429 in statuses


def test_tool_concurrency_limit_holds_slot_until_completion(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import monzo_backend.mcp.server as server_module

    ready = threading.Barrier(3)
    release = threading.Event()
    original = server_module.execute

    def blocked(*args):
        ready.wait(timeout=5)
        assert release.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(server_module, "execute", blocked)
    settings = auth_settings(tmp_path)
    with (
        TestClient(
            create_app(settings, engine=engine(), verifier=AcceptedTestToken()),
            base_url="https://spending.example.invalid",
        ) as client,
        ThreadPoolExecutor(2) as pool,
    ):

        def call():
            return client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer test-fixture",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_summary", "arguments": SCOPE},
                },
            ).json()["result"]

        futures = [pool.submit(call) for _ in range(2)]
        try:
            ready.wait(timeout=5)
            assert call()["structuredContent"]["error"]["code"] == "rate_limited"
        finally:
            release.set()
        assert all(f.result(timeout=5).get("isError", False) is False for f in futures)
