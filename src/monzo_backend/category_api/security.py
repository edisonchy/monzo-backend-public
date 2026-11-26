"""Independent verification of category-audience tokens; no credential fallback."""

from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from monzo_backend.category_changes import CategoryActor
from monzo_backend.mcp.security import AuthFailure, TokenVerifier

READ = "finance:read"
WRITE = "finance:categories:write"


class CommandSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONZO_CATEGORY_", extra="ignore")
    enabled: bool = False
    batches_enabled: bool = False
    database_url: SecretStr = SecretStr("")
    issuer: str = ""
    jwks_url: str = ""
    owner_subject: str = ""
    exchange_client: str = "https://spending.example.invalid/mcp"
    resource: str = "finance-category-api"

    @model_validator(mode="after")
    def configured(self):
        if self.resource != "finance-category-api":
            raise ValueError("Category API audience is fixed")
        if not self.enabled:
            return self
        issuer, jwks = (urlsplit(v) for v in (self.issuer, self.jwks_url))
        for url in (issuer, jwks):
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("Issuer and JWKS must be fixed HTTPS URLs")
        if issuer.netloc != jwks.netloc or not jwks.path.startswith(issuer.path.rstrip("/") + "/"):
            raise ValueError("JWKS must belong to the configured issuer")
        if (
            not self.owner_subject
            or not self.exchange_client
            or not self.database_url.get_secret_value()
        ):
            raise ValueError(
                "Category API requires owner, exchange client and database configuration"
            )
        return self


class CommandVerifier(TokenVerifier):
    async def authorize(self, header, *, write):
        # The existing verifier checks signature, RS256/kid, issuer, exact single
        # audience, expiry, owner and finance:read. It fetches only configured
        # HTTPS JWKS with bounded bodies, no redirects and no environment proxy.
        claims = await self.verify(header)
        if (
            type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not 0 < claims["exp"] - claims["iat"] <= 60
            or claims.get("typ") != "Bearer"
        ):
            raise AuthFailure(401)
        if claims.get("azp") != self.settings.exchange_client:
            raise AuthFailure(403)
        if write and WRITE not in claims["scope"].split():
            raise AuthFailure(403)
        # This is the authenticated exchange requester, not a caller-supplied
        # originating client. Gate A proved Keycloak uses this azp on exchange.
        return CategoryActor(claims["iss"], claims["sub"], claims["azp"]), claims
