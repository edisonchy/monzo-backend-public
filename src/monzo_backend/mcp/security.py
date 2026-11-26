"""Owner-bound resource-server authentication. No OAuth grants or passwords here."""

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("monzo.mcp.auth")


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONZO_MCP_", extra="ignore")
    enabled: bool = False
    categories_enabled: bool = False
    exchange_secret: SecretStr = SecretStr("")
    category_ca_file: Path = Path("/run/category-ca.pem")
    database_url: SecretStr = SecretStr("")
    cursor_secret: SecretStr = SecretStr("")
    resource: str = "https://spending.example.invalid/mcp"
    issuer: str = ""
    jwks_url: str = ""
    owner_subject: str = ""
    enable_file: Path = Path("/run/monzo-mcp/access")
    algorithm: str = Field("RS256", pattern=r"^RS256$")

    @model_validator(mode="after")
    def configured(self):
        if not self.enabled:
            return self
        urls = [urlsplit(v) for v in (self.resource, self.issuer, self.jwks_url)]
        for url in urls:
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("MCP resource, issuer and JWKS must be fixed HTTPS URLs")
        if urls[0].path != "/mcp" or urls[1].netloc != urls[2].netloc:
            raise ValueError("Use /mcp and a JWKS endpoint on the configured issuer host")
        if (
            not self.owner_subject
            or not self.database_url.get_secret_value()
            or len(self.cursor_secret.get_secret_value()) < 32
        ):
            raise ValueError(
                "MCP requires owner subject, database URL and a separate random cursor secret"
            )
        if self.categories_enabled and (
            not self.exchange_secret.get_secret_value() or not self.category_ca_file.is_file()
        ):
            raise ValueError(
                "Category tools require a separate exchange secret and trusted CA file"
            )
        return self

    def access_enabled(self):
        try:
            return self.enabled and self.enable_file.read_bytes() == b"enabled\n"
        except OSError:
            return False


class AuthFailure(Exception):
    def __init__(self, status):
        self.status = status


class TokenVerifier:
    def __init__(self, settings):
        self.settings = settings
        self.keys = {}
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def public_keys(self):
        if time.monotonic() < self.expires:
            return self.keys
        async with self.lock:
            if time.monotonic() < self.expires:
                return self.keys
            try:
                async with httpx.AsyncClient(
                    timeout=3, follow_redirects=False, trust_env=False
                ) as client:
                    async with client.stream("GET", self.settings.jwks_url) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 65536:
                                raise ValueError("JWKS too large")
                import json

                values = json.loads(body)["keys"]
                keys = {}
                for item in values:
                    if (
                        item.get("kty") != "RSA"
                        or item.get("use", "sig") != "sig"
                        or item.get("alg", "RS256") != "RS256"
                    ):
                        continue
                    key = jwt.PyJWK.from_dict(item, algorithm="RS256").key
                    kid = item.get("kid")
                    if not isinstance(kid, str) or not kid or kid in keys or key.key_size < 2048:
                        raise ValueError("Invalid signing key")
                    keys[kid] = key
                if not keys:
                    raise ValueError("No usable keys")
                self.keys, self.expires = keys, time.monotonic() + 60
            except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError):
                # Cache failure briefly to bound network calls during provider outage.
                self.keys, self.expires = {}, time.monotonic() + 5
            return self.keys

    async def verify(self, header):
        if not header or not header.startswith("Bearer ") or len(header) > 8192:
            raise AuthFailure(401)
        token = header[7:]
        try:
            header_data = jwt.get_unverified_header(token)
            if header_data.get("alg") != "RS256" or header_data.get("crit"):
                raise ValueError()
            kid = header_data.get("kid")
            if not isinstance(kid, str):
                raise ValueError()
            keys = await self.public_keys()
            if kid not in keys:
                raise ValueError()
            claims = jwt.decode(
                token,
                keys[kid],
                algorithms=["RS256"],
                issuer=self.settings.issuer,
                audience=self.settings.resource,
                options={"require": ["exp", "iat", "iss", "aud", "sub"], "strict_aud": True},
            )
            if (
                type(claims["exp"]) is not int
                or type(claims["iat"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= 300
            ):
                raise ValueError()
            scope = claims.get("scope", "")
            if (
                claims["sub"] != self.settings.owner_subject
                or not isinstance(scope, str)
                or "finance:read" not in scope.split()
            ):
                raise AuthFailure(403)
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            # Class names identify configuration faults without exposing tokens,
            # claims, key material or exception messages in operational logs.
            log.warning("mcp_auth_rejected reason=%s", type(exc).__name__)
            raise AuthFailure(401) from None
