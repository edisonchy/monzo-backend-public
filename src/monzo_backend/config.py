import re
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import IPvAnyAddress, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MONZO_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: SecretStr
    owner_token: SecretStr
    automation_token: SecretStr
    encryption_key: SecretStr
    webhook_secret: SecretStr
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    redirect_uri: str = "http://localhost:8087/oauth/callback"
    public_webhook_url: str = ""
    api_url: str = "https://api.monzo.com"
    proxy_url: str = "http://cliproxyapi:8317/v1"
    proxy_key: SecretStr = SecretStr("")
    model: str = ""
    dashboard_trusted_proxy_ips: list[IPvAnyAddress] = []
    dashboard_origin: str = ""
    dashboard_password_hash: SecretStr = SecretStr("")
    ai_enabled: bool = False
    ai_auto_accept: bool = False
    web_search_enabled: bool = False
    search_timeout: float = 60.0
    request_timeout: float = 30.0
    job_lease_seconds: int = 180
    max_attempts: int = 3
    retry_base_seconds: int = 15
    secure_cookies: bool = True

    @model_validator(mode="after")
    def check_security(self):
        for name in ("owner_token", "automation_token"):
            if len(getattr(self, name).get_secret_value()) < 32:
                raise ValueError(f"{name} must have at least 32 characters")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.webhook_secret.get_secret_value()):
            raise ValueError("webhook_secret must be 32 to 128 URL-safe letters, digits, _ or -")
        if self.owner_token == self.automation_token:
            raise ValueError("Owner and automation credentials must differ")
        Fernet(self.encryption_key.get_secret_value().encode())
        if self.dashboard_origin:
            from urllib.parse import urlsplit

            origin = urlsplit(self.dashboard_origin)
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.path
                or origin.query
                or origin.fragment
                or origin.username
            ):
                raise ValueError("Dashboard origin must be an HTTPS origin without a path")
        password_hash = self.dashboard_password_hash.get_secret_value()
        if bool(self.dashboard_origin) != bool(password_hash):
            raise ValueError("Dashboard origin and password hash must be configured together")
        if password_hash and not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{128}", password_hash):
            raise ValueError("Invalid dashboard password hash format")
        if self.request_timeout <= 0 or self.search_timeout <= 0:
            raise ValueError("HTTP deadlines must be positive")
        budget = self.request_timeout * 2 + (self.search_timeout if self.web_search_enabled else 0)
        if self.job_lease_seconds < budget + 10:
            raise ValueError("Job lease must exceed the total HTTP deadline")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
