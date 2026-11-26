"""Gate C: signed resource tokens and a real isolated PostgreSQL command service."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import func, select, text
from starlette.testclient import TestClient

from monzo_backend import category_operations as ops
from monzo_backend.category_api import app as api
from monzo_backend.category_api.database import command_engine, sessions
from monzo_backend.category_api.limits import RateLimited, admit
from monzo_backend.category_api.security import CommandSettings, CommandVerifier
from monzo_backend.category_changes import CategoryActor
from monzo_backend.db import engine, session
from monzo_backend.domain import CATEGORIES, ingest
from monzo_backend.models import (
    CategoryOperation,
    CategoryRateLimit,
    CategoryWriteControl,
    Transaction,
)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER = "https://identity.test/realms/finance"
CLIENT = "https://spending.example.invalid/mcp"
ACTOR = CategoryActor(ISSUER, "test-owner", CLIENT)
PREFIX = api.PREFIX
CHANGE = {"transaction_id": "tx_test", "category": "groceries", "expected_version": 1}


def token(**extra):
    stamp = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "test-owner",
        "aud": "finance-category-api",
        "azp": CLIENT,
        "typ": "Bearer",
        "iat": stamp,
        "exp": stamp + 60,
        "scope": "finance:read finance:categories:write",
        **extra,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return "Bearer " + jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})


def config(**extra):
    values = dict(
        enabled=True,
        database_url=engine().url.render_as_string(hide_password=False),
        issuer=ISSUER,
        jwks_url=ISSUER + "/protocol/openid-connect/certs",
        owner_subject="test-owner",
    )
    return CommandSettings(**(values | extra))


def verified(settings):
    verifier = CommandVerifier(settings)
    verifier.keys = {"test-key": KEY.public_key()}
    verifier.expires = time.monotonic() + 60
    return verifier


def build(settings=None):
    settings = settings or config()
    return api.create_app(
        settings,
        engine=command_engine(settings.database_url.get_secret_value()),
        verifier=verified(settings),
    )


@pytest.fixture
def ready(payment):
    with session() as s, s.begin():
        s.add(CategoryWriteControl(id=1, enabled=True))
        tx = ingest(s, "acc_test", payment)
        tx.category, tx.category_source, tx.category_status = "shopping", "ai", "needs_review"


@pytest.fixture
def client_api(ready):
    with TestClient(
        build(), base_url="https://category-api", headers={"Authorization": token()}
    ) as client:
        yield client


def post(client, path, body=None, **kwargs):
    return client.post(PREFIX + path, json=body if body is not None else {}, **kwargs)


def test_private_command_flow_and_disable_recovery(client_api):
    client = client_api
    assert client.get(PREFIX + "/options").json()["can_write"]
    p = post(client, "/previews", CHANGE)
    assert p.status_code == 201, p.text
    op = p.json()["operation_id"]
    r = post(client, f"/operations/{op}/apply")
    assert r.status_code == 200, r.text
    receipt = r.json()["receipt"]
    u = post(client, "/undo-previews", {"batch_id": receipt["batch_id"]})
    assert u.status_code == 201, u.text
    inverse = post(client, f"/operations/{u.json()['operation_id']}/undo")
    assert inverse.status_code == 200, inverse.text
    with session() as s, s.begin():
        ops.set_writes_enabled(s, False)
    # Receipt lookup never consumes the exhausted commit burst budget.
    assert client.get(PREFIX + f"/operations/{op}").json()["receipt"] == receipt
    assert client.get(PREFIX + "/options").json()["writes_enabled"] is False
    assert (
        post(client, "/previews", {**CHANGE, "expected_version": 3}).json()["error"]
        == "writes_disabled"
    )
    with session() as s:
        assert s.get(Transaction, "tx_test").category == "shopping"


@pytest.mark.parametrize(
    "claims,status",
    [
        ({"aud": CLIENT}, 401),
        ({"aud": ["finance-category-api", CLIENT]}, 401),
        ({"aud": ["finance-category-api"]}, 401),
        ({"aud": "account"}, 401),
        ({"iss": "https://other.test"}, 401),
        ({"sub": "different-owner"}, 403),
        ({"azp": "automation-client"}, 403),
        ({"azp": None}, 403),
        ({"scope": "finance:read"}, 403),
        ({"scope": "finance:categories:write"}, 403),
        ({"typ": "ID"}, 401),
        (lambda: {"exp": int(time.time()) - 1}, 401),
        (lambda: {"exp": int(time.time()) + 120}, 401),
        ({"iat": None}, 401),
        ({"exp": None}, 401),
        ({"iat": True}, 401),
        (lambda: {"iat": int(time.time()) + 30, "exp": int(time.time()) + 60}, 401),
    ],
)
def test_wrong_tokens_denied_without_operation_or_admission(client_api, claims, status):
    # Evaluate at request time; earlier tests must not shorten the intended lifetime.
    if callable(claims):
        claims = claims()
    r = post(client_api, "/previews", CHANGE, headers={"Authorization": token(**claims)})
    assert r.status_code == status, r.text
    assert "WWW-Authenticate" in r.headers
    with session() as s:
        assert s.scalar(select(func.count()).select_from(CategoryOperation)) == 0
        assert s.scalar(select(func.count()).select_from(CategoryRateLimit)) == 0
        assert s.get(Transaction, "tx_test").version == 1


@pytest.mark.parametrize(
    "auth",
    [
        "",
        "Bearer synthetic-owner",
        "Bearer synthetic-automation",
        "Basic anything",
        "Bearer " + "x" * 9000,
    ],
)
def test_legacy_credentials_never_authorize(client_api, auth):
    r = post(
        client_api,
        "/previews",
        CHANGE,
        headers={
            "Authorization": auth,
            "Cookie": "__Host-monzo_session=synthetic",
            "X-Owner": "test-owner",
            "X-Client": CLIENT,
        },
    )
    assert r.status_code == 401


def test_forged_signature_and_untrusted_key_url_denied(client_api):
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(token()[7:], options={"verify_signature": False})
    signed = jwt.encode(
        claims,
        wrong,
        algorithm="RS256",
        headers={"kid": "test-key", "jku": "https://attacker.test/key"},
    )
    assert (
        post(
            client_api, "/previews", CHANGE, headers={"Authorization": "Bearer " + signed}
        ).status_code
        == 401
    )


def test_options_read_scope_cannot_create_or_lookup_operations(client_api):
    client = client_api
    headers = {"Authorization": token(scope="finance:read")}
    r = client.get(PREFIX + "/options", headers=headers)
    assert r.status_code == 200
    assert [v["id"] for v in r.json()["categories"]] == list(CATEGORIES)
    assert r.json()["writes_enabled"] and not r.json()["can_write"]
    assert post(client, "/previews", CHANGE, headers=headers).status_code == 403
    assert (
        client.get(
            PREFIX + "/operations/00000000-0000-0000-0000-000000000000", headers=headers
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"owner_subject": "forged"},
        {"scope": "all"},
        {"future_rule": True},
        {"user_confirmed": True},
        {"expected_version": True},
    ],
)
def test_strict_request_models(client_api, extra):
    assert post(client_api, "/previews", {**CHANGE, **extra}).status_code == 422


def test_apply_cannot_replace_prepared_parameters(client_api):
    op = post(client_api, "/previews", CHANGE).json()["operation_id"]
    assert post(client_api, f"/operations/{op}/apply", {"category": "bills"}).status_code == 422
    assert post(client_api, f"/operations/{op}/apply?category=bills").status_code == 422
    assert post(client_api, f"/operations/{op}/undo").json()["error"] == "operation_kind_mismatch"
    assert post(client_api, f"/operations/{op}/apply").status_code == 200


def test_body_transport_and_route_boundaries(client_api):
    client = client_api
    assert (
        client.post(
            PREFIX + "/previews", content=b"x" * 65537, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )
    assert (
        client.post(
            PREFIX + "/previews", content="{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    for path in [
        "/v1/status",
        "/v1/processing/resume",
        "/v1/dashboard/transactions",
        "/oauth/start",
        "/docs",
        "/openapi.json",
        PREFIX + "/previews/",
    ]:
        assert client.get(path, follow_redirects=False).status_code == 404
        assert client.post(path, json={}, follow_redirects=False).status_code == 404
    assert (
        client.get(
            "http://category-api" + PREFIX + "/options", headers={"X-Forwarded-Proto": "https"}
        ).status_code
        == 400
    )
    assert (
        client.get(PREFIX + "/options", headers={"Host": "spending.example.invalid"}).status_code
        == 400
    )
    assert (
        client.post(
            PREFIX + "/previews",
            json=CHANGE,
            headers=[("Authorization", token()), ("Authorization", token())],
        ).status_code
        == 401
    )
    assert client.get(PREFIX + "/options").headers["cache-control"] == "no-store"


def test_disabled_service_and_missing_write_control_fail_closed(ready):
    settings = config().model_copy(update={"enabled": False})
    with TestClient(build(settings), base_url="https://category-api") as client:
        assert client.get(PREFIX + "/options").json()["error"] == "service_disabled"
    with session() as s, s.begin():
        s.delete(s.get(CategoryWriteControl, 1))
    with TestClient(
        build(), base_url="https://category-api", headers={"Authorization": token()}
    ) as client:
        assert post(client, "/previews", CHANGE).json()["error"] == "writes_disabled"


def test_jwks_discovery_is_fixed_bounded_and_fails_closed(ready):
    settings = config()
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())) | {
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
    }
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(settings.jwks_url).mock(
            return_value=httpx.Response(200, json={"keys": [key]})
        )
        verifier = CommandVerifier(settings)
        actor, _ = asyncio.run(verifier.authorize(token(), write=True))
        assert actor == ACTOR and route.call_count == 1
        assert asyncio.run(verifier.authorize(token(), write=True))[0] == ACTOR
        assert route.call_count == 1
        for bad in [
            httpx.Response(302, headers={"Location": "https://attacker.test/keys"}),
            httpx.Response(200, content=b"x" * 65537),
            httpx.Response(503),
        ]:
            route.mock(return_value=bad)
            with TestClient(
                api.create_app(
                    settings, engine=command_engine(settings.database_url.get_secret_value())
                ),
                base_url="https://category-api",
                headers={"Authorization": token()},
            ) as client:
                assert post(client, "/previews", CHANGE).status_code == 401
    with pytest.raises(ValidationError):
        config(jwks_url="http://identity.test/keys")


def test_limits_shared_across_instances_and_receipt_lookup_exempt(client_api):
    client = client_api
    first = post(client, "/previews", CHANGE).json()["operation_id"]
    with TestClient(
        build(), base_url="https://category-api", headers={"Authorization": token()}
    ) as second:
        for i in range(9):
            assert post(client if i % 2 else second, "/previews", CHANGE).status_code == 201
        denied = post(second, "/previews", CHANGE)
        assert denied.status_code == 429 and int(denied.headers["retry-after"]) > 0
        assert second.get(PREFIX + f"/operations/{first}").status_code == 200
    with session() as s:
        assert s.scalar(select(func.count()).select_from(CategoryRateLimit)) == 1


def test_commit_burst_and_rolling_minute_budget(ready):
    factory = sessions(
        command_engine(config().database_url.get_secret_value()), time.monotonic() + 10
    )
    for _ in range(2):
        admit(factory, ACTOR, "commit")
    with pytest.raises(RateLimited):
        admit(factory, ACTOR, "commit")
    # Model elapsed refill time without sleeping or changing process clocks.
    for _ in range(3):
        with session() as s, s.begin():
            row = s.scalar(select(CategoryRateLimit))
            row.updated_at -= timedelta(seconds=12)
        admit(factory, ACTOR, "commit")
    with session() as s, s.begin():
        s.scalar(select(CategoryRateLimit)).updated_at -= timedelta(seconds=30)
    with pytest.raises(RateLimited):
        admit(factory, ACTOR, "commit")
    with session() as s:
        assert len(s.scalar(select(CategoryRateLimit)).attempts) == 5


def test_concurrent_admission_cannot_overspend_burst(ready):
    db = command_engine(config().database_url.get_secret_value())

    def attempt(_):
        try:
            admit(sessions(db, time.monotonic() + 10), ACTOR, "commit")
            return True
        except RateLimited:
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(attempt, range(4))) == 2
    db.dispose()


def test_db_deadline_rolls_back_without_driver_details(client_api, monkeypatch):
    original = ops.prepare_change

    def slow(s, *args):
        result = original(s, *args)
        s.execute(text("SELECT pg_sleep(1)"))
        return result

    monkeypatch.setattr(ops, "prepare_change", slow)
    monkeypatch.setattr(api, "DEADLINE", 0.15)
    r = post(client_api, "/previews", CHANGE)
    assert r.status_code == 503 and r.json()["error"] == "outcome_unknown"
    # TestClient shutdown drains SQL work, but explicitly wait for the rollback
    # here to prove no prepared record survives the canceled transaction.
    time.sleep(0.2)
    with session() as s:
        assert s.scalar(select(func.count()).select_from(CategoryOperation)) == 0


def test_expired_token_cannot_commit_after_waiting(client_api, monkeypatch):
    original = ops.prepare_change

    def delayed(s, *args):
        result = original(s, *args)
        time.sleep(2.1)
        return result

    monkeypatch.setattr(ops, "prepare_change", delayed)
    r = post(
        client_api, "/previews", CHANGE, headers={"Authorization": token(exp=int(time.time()) + 2)}
    )
    assert r.status_code == 503
    with session() as s:
        assert s.scalar(select(func.count()).select_from(CategoryOperation)) == 0


def test_timeout_retains_work_slots_until_threads_finish(ready, monkeypatch):
    release = Event()

    def stalled(*args):
        release.wait(3)
        return {}, 200

    monkeypatch.setattr(api, "execute", stalled)
    monkeypatch.setattr(api, "DEADLINE", 0.1)

    async def run():
        app = build()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://category-api",
                headers={"Authorization": token()},
            ) as client:
                responses = await asyncio.gather(
                    *[client.post(PREFIX + "/previews", json=CHANGE) for _ in range(2)]
                )
                assert all(r.json()["error"] == "outcome_unknown" for r in responses)
                assert len(app.active) == 2
                third = await client.post(PREFIX + "/previews", json=CHANGE)
                assert third.json()["error"] == "busy"
        finally:
            release.set()
            app.pool.shutdown(wait=True)
            app.engine.dispose()

    asyncio.run(run())


def test_post_commit_failure_is_unknown_and_receipt_remains_readable(client_api, monkeypatch):
    operation = post(client_api, "/previews", CHANGE).json()["operation_id"]
    original = ops.get_operation

    def failed_read(*args):
        raise RuntimeError("sensitive driver detail must never be returned")

    monkeypatch.setattr(ops, "get_operation", failed_read)
    result = post(client_api, f"/operations/{operation}/apply")
    assert result.status_code == 503
    assert result.json() == {"error": "outcome_unknown"}
    monkeypatch.setattr(ops, "get_operation", original)
    receipt = client_api.get(PREFIX + f"/operations/{operation}")
    assert receipt.json()["receipt"]["changed"] == 1
    assert (
        post(client_api, f"/operations/{operation}/apply").json()["receipt"]
        == receipt.json()["receipt"]
    )


def test_batch_endpoint_is_disabled_by_default(payment):
    with session() as s, s.begin():
        ingest(s, "acc_test", payment)
        s.add(CategoryWriteControl(id=1, enabled=True))
    app = build()
    with TestClient(app, base_url="https://category-api") as client:
        r = client.post(
            PREFIX + "/batch-previews",
            headers={"Authorization": token()},
            json={**CHANGE, "merchant_id": "merch_test"},
        )
        assert r.json()["error"] == "batches_disabled"
        options = client.get(PREFIX + "/options", headers={"Authorization": token()}).json()
        assert options["batch_scopes"] == [] and not options["future_rules"]
