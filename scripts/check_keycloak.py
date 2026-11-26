"""Private commissioning check against real Keycloak and MCP services.

Requires an administrator token in a mode-0600 file, SSH tunnels to the identity
private listener and MCP, and temporarily enabled MCP access. Creates only a
uniquely named test client/user, deletes both in finally, and prints no tokens or
financial responses. Never weakens the owner's MFA or production client.
"""

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

ORIGIN = "https://spending.example.invalid"
ISSUER = ORIGIN + "/auth/realms/finance"
RESOURCE = ORIGIN + "/mcp"
CALLBACK = "http://127.0.0.1:18765/callback"


class Form(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.action = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.action = dict(attrs).get("action")


class Tunnel(httpx.BaseTransport):
    def __init__(self, url):
        self.url = httpx.URL(url)
        self.transport = httpx.HTTPTransport()

    def handle_request(self, request):
        if request.url.host != "spending.example.invalid":
            raise ValueError("Unexpected OAuth redirect host")
        url = request.url.copy_with(scheme=self.url.scheme, host=self.url.host, port=self.url.port)
        return self.transport.handle_request(
            httpx.Request(
                request.method,
                url,
                headers=request.headers,
                content=request.read(),
                extensions=request.extensions,
            )
        )

    def close(self):
        self.transport.close()


def secret_file(path):
    if path.stat().st_mode & 0o077:
        raise ValueError("Token file must have mode 0600")
    return path.read_text().strip()


def run(args):
    for url in (args.identity_tunnel, args.mcp_tunnel):
        if urlsplit(url).hostname != "127.0.0.1":
            raise ValueError("Use SSH loopback tunnels for commissioning")
    admin = httpx.Client(
        base_url=args.identity_tunnel + "/auth/admin/realms/finance/",
        headers={"Authorization": "Bearer " + secret_file(args.admin_token_file)},
        timeout=20,
        trust_env=False,
    )

    def api(method, path, body=None):
        response = admin.request(method, path, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"Private admin {method} failed: HTTP {response.status_code}")
        return response.json() if response.content else None

    realm = api("GET", "")
    assert not realm["registrationAllowed"] and realm["bruteForceProtected"]
    assert realm["accessTokenLifespan"] <= 300
    assert realm["revokeRefreshToken"] and realm["refreshTokenMaxReuse"] == 0
    assert 0 < realm["offlineSessionIdleTimeout"] <= 2592000
    assert realm["offlineSessionMaxLifespanEnabled"]
    assert 0 < realm["offlineSessionMaxLifespan"] <= 365 * 24 * 60 * 60
    production_client = api("GET", "clients?clientId=chatgpt-finance")[0]
    scopes = api("GET", f"clients/{production_client['id']}/optional-client-scopes")
    assert {"finance:read", "offline_access"} <= {s["name"] for s in scopes}
    executions = api("GET", "authentication/flows/owner-password-and-otp/executions")
    assert all(
        any(e.get("providerId") == provider and e["requirement"] == "REQUIRED" for e in executions)
        for provider in ("auth-username-password-form", "auth-otp-form")
    )
    print(
        "PASS realm policy: no signup, mandatory password and OTP, short tokens, refresh rotation"
    )
    name = "acceptance-" + secrets.token_hex(8)
    password, client_secret, otp_secret = (secrets.token_urlsafe(32) for _ in range(3))
    client_id = user_id = None
    try:
        api(
            "POST",
            "clients",
            {
                "clientId": name,
                "enabled": True,
                "publicClient": False,
                "secret": client_secret,
                "protocol": "openid-connect",
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False,
                "serviceAccountsEnabled": False,
                "fullScopeAllowed": False,
                "redirectUris": [CALLBACK],
                "webOrigins": [],
                "defaultClientScopes": [],
                "optionalClientScopes": ["finance:read", "offline_access"],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "access.token.lifespan": "300",
                },
                "protocolMappers": [
                    {
                        "name": "subject",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-sub-mapper",
                        "config": {"access.token.claim": "true", "id.token.claim": "true"},
                    }
                ],
            },
        )
        client_id = api("GET", "clients?clientId=" + name)[0]["id"]
        api(
            "POST",
            "users",
            {
                "username": name,
                "enabled": True,
                "firstName": "Acceptance",
                "lastName": "Test",
                "email": name + "@example.invalid",
                "emailVerified": True,
                "credentials": [
                    {"type": "password", "value": password, "temporary": False},
                    {
                        "type": "otp",
                        "userLabel": "Disposable acceptance test",
                        "secretData": json.dumps({"value": otp_secret}),
                        "credentialData": json.dumps(
                            {
                                "subType": "totp",
                                "digits": 6,
                                "counter": 0,
                                "period": 30,
                                "algorithm": "HmacSHA1",
                            }
                        ),
                    },
                ],
            },
        )
        user_id = api("GET", "users?username=" + name + "&exact=true")[0]["id"]
        offline_role = api("GET", "roles/offline_access")
        api("POST", f"users/{user_id}/role-mappings/realm", [offline_role])
        with httpx.Client(transport=Tunnel(args.identity_tunnel), timeout=20) as browser:
            metadata = browser.get(ISSUER + "/.well-known/openid-configuration").json()
            assert metadata["issuer"] == ISSUER
            assert "S256" in metadata["code_challenge_methods_supported"]
            assert metadata["authorization_response_iss_parameter_supported"]
            verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(1536)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            params = {
                "client_id": name,
                "redirect_uri": CALLBACK,
                "response_type": "code",
                "scope": "openid offline_access finance:read",
                "resource": RESOURCE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            for patch in (
                {"redirect_uri": "https://example.invalid/callback"},
                {"code_challenge": ""},
            ):
                response = browser.get(
                    metadata["authorization_endpoint"], params={**params, **patch}
                )
                assert response.status_code in (400, 302) and not Form(response.text).action
                if response.status_code == 302:
                    assert "error=" in response.headers["location"]
            response = browser.get(metadata["authorization_endpoint"], params=params)
            response = browser.post(
                Form(response.text).action, data={"username": name, "password": password}
            )
            assert response.status_code == 200 and 'name="otp"' in response.text
            response = browser.post(Form(response.text).action, data={"otp": "invalid"})
            assert response.status_code == 200 and 'name="otp"' in response.text
            mac = hmac.new(
                otp_secret.encode(), struct.pack(">Q", int(time.time()) // 30), hashlib.sha1
            ).digest()
            offset = mac[-1] & 15
            otp = str(
                (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
            ).zfill(6)
            response = browser.post(Form(response.text).action, data={"otp": otp})
            assert response.status_code == 302
            query = parse_qs(urlsplit(response.headers["location"]).query)
            assert query["state"] == [state] and query["iss"] == [ISSUER]
            grant = {
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "code_verifier": verifier,
                "redirect_uri": CALLBACK,
                "client_id": name,
                "client_secret": client_secret,
            }
            response = browser.post(metadata["token_endpoint"], data=grant)
            assert response.status_code == 200
            tokens = response.json()
            access_payload = tokens["access_token"].split(".")[1]
            access_claims = json.loads(
                base64.urlsafe_b64decode(access_payload + "=" * (-len(access_payload) % 4))
            )
            assert access_claims["aud"] == RESOURCE
            assert {"openid", "offline_access", "finance:read"} <= set(tokens["scope"].split())
            # Inspect only the disposable test grant, never the owner's browser tokens.
            refresh_payload = tokens["refresh_token"].split(".")[1]
            refresh_claims = json.loads(
                base64.urlsafe_b64decode(refresh_payload + "=" * (-len(refresh_payload) % 4))
            )
            assert refresh_claims["typ"] == "Offline"
            assert 0 < tokens["refresh_expires_in"] <= realm["offlineSessionIdleTimeout"]
            refresh = {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": name,
                "client_secret": client_secret,
            }
            refreshed = browser.post(metadata["token_endpoint"], data=refresh)
            assert refreshed.status_code == 200
            refreshed_payload = refreshed.json()["access_token"].split(".")[1]
            refreshed_claims = json.loads(
                base64.urlsafe_b64decode(refreshed_payload + "=" * (-len(refreshed_payload) % 4))
            )
            assert refreshed_claims["aud"] == RESOURCE
            assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
            assert browser.post(metadata["token_endpoint"], data=refresh).status_code == 400
            assert browser.post(metadata["token_endpoint"], data=grant).status_code == 400
            print(
                "PASS real OAuth: callback/PKCE checks, required MFA, issuer, code replay and refresh rotation"
            )
            if args.identity_only:
                print("Identity-only check: MCP acceptance was not run")
                return
            with httpx.Client(
                base_url=args.mcp_tunnel,
                timeout=20,
                trust_env=False,
                headers={
                    "Host": "spending.example.invalid",
                    "Accept": "application/json, text/event-stream",
                },
            ) as mcp:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                assert mcp.post("/mcp", json=payload).status_code == 401
                response = mcp.post(
                    "/mcp",
                    json=payload,
                    headers={"Authorization": "Bearer " + tokens["access_token"]},
                )
                assert response.status_code == 403
                if args.owner_token_file:
                    response = mcp.post(
                        "/mcp",
                        json=payload,
                        headers={"Authorization": "Bearer " + secret_file(args.owner_token_file)},
                    )
                    assert (
                        response.status_code == 200
                        and len(response.json()["result"]["tools"]) == 10
                    )
                print(
                    "PASS deployed MCP: anonymous denied; signed, MFA-authenticated non-owner denied"
                )
    finally:
        try:
            if user_id:
                api("DELETE", "users/" + user_id)
        finally:
            if client_id:
                api("DELETE", "clients/" + client_id)
            admin.close()
        print("Removed temporary acceptance identities")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-token-file", type=Path, required=True)
    parser.add_argument("--owner-token-file", type=Path)
    parser.add_argument("--identity-tunnel", default="http://127.0.0.1:18182")
    parser.add_argument("--mcp-tunnel", default="http://127.0.0.1:18183")
    parser.add_argument("--identity-only", action="store_true")
    run(parser.parse_args())
