import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest


@pytest.fixture(autouse=True)
def clean_database():
    """Override the database fixture: this check only uses mocked HTTP."""


@pytest.mark.parametrize(
    ("updates", "accepted"),
    [
        ({"offlineSessionMaxLifespan": 31536000}, True),
        ({"offlineSessionMaxLifespan": 31536001}, False),
        ({"offlineSessionMaxLifespan": 0}, False),
        ({"offlineSessionMaxLifespan": -1}, False),
        ({"offlineSessionMaxLifespanEnabled": False}, False),
    ],
)
def test_commissioning_accepts_configured_lifetime_and_keeps_bound(monkeypatch, updates, accepted):
    realm = json.loads(Path("deploy/keycloak/finance-realm.json").read_text()) | updates
    run = runpy.run_path("scripts/check_keycloak.py")["run"]
    monkeypatch.setitem(run.__globals__, "secret_file", lambda _: "synthetic-token")

    class PolicyPassed(Exception):
        pass

    def request(method, path, **kwargs):
        assert method == "GET"
        if path == "":
            return httpx.Response(200, json=realm)
        assert path == "clients?clientId=chatgpt-finance"
        raise PolicyPassed  # Stop before any authentication or identity mutations.

    admin = Mock()
    admin.request.side_effect = request
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: admin)
    args = SimpleNamespace(
        identity_tunnel="http://127.0.0.1:18182",
        mcp_tunnel="http://127.0.0.1:18183",
        admin_token_file=None,
    )
    with pytest.raises(PolicyPassed if accepted else AssertionError):
        run(args)
    assert admin.request.call_count == (2 if accepted else 1)
