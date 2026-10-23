from functools import lru_cache
from urllib.parse import parse_qs, urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    keycloak_issuer_url: str = "https://ouros-keycloak.discloud.app/realms/ouros"
    keycloak_audience: str = "ms-ouros-audience-engine"
    keycloak_jwks_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    mongodb_uri: str = "mongodb://localhost:27017"
    audience_database_name: str = Field(
        default="ouros_audience", min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_-]+$"
    )
    source_database_url: str = ""
    metadata_database_url: str = ""
    source_timeout_seconds: int = Field(default=5, ge=1, le=30)
    allow_manual_run: bool = False

    @model_validator(mode="after")
    def require_production_secrets(self) -> "Settings":
        if self.environment.lower() != "production":
            return self
        issuer = urlparse(self.keycloak_issuer_url)
        if issuer.scheme != "https" or not issuer.hostname or issuer.username or issuer.password:
            raise ValueError("KEYCLOAK_ISSUER_URL must use HTTPS")
        if not self.keycloak_audience.strip():
            raise ValueError("KEYCLOAK_AUDIENCE is required in production")
        if self.keycloak_jwks_url:
            jwks = urlparse(self.keycloak_jwks_url)
            if jwks.scheme != "https" or not jwks.hostname or jwks.username or jwks.password:
                raise ValueError("KEYCLOAK_JWKS_URL must use HTTPS")
        for name, value in (
            ("SOURCE_DATABASE_URL", self.source_database_url),
            ("METADATA_DATABASE_URL", self.metadata_database_url),
        ):
            parsed = urlparse(value)
            sslmode = parse_qs(parsed.query).get("sslmode", [""])[0]
            if parsed.scheme not in {"postgres", "postgresql"} or sslmode not in {
                "verify-ca",
                "verify-full",
            }:
                raise ValueError(f"{name} must use PostgreSQL TLS with certificate verification")
        if urlparse(self.redis_url).scheme != "rediss":
            raise ValueError("REDIS_URL must use rediss:// in production")
        mongo_uri = urlparse(self.mongodb_uri)
        if (
            mongo_uri.scheme not in {"mongodb", "mongodb+srv"}
            or not mongo_uri.hostname
            or not mongo_uri.username
            or not mongo_uri.password
        ):
            raise ValueError("MONGODB_URI must include a host and database credentials")
        if mongo_uri.scheme == "mongodb":
            options = parse_qs(mongo_uri.query)
            tls_value = options.get("tls", options.get("ssl", ["false"]))[0].lower()
            if tls_value not in {"true", "1"}:
                raise ValueError("MONGODB_URI must use TLS in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
