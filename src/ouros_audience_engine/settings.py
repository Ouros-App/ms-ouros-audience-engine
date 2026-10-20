from functools import lru_cache
from urllib.parse import parse_qs, urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    service_token: str = ""
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint_url: str = "http://localhost:9000"
    source_database_url: str = ""
    metadata_database_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = "ouros-audience"
    s3_prefix: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_use_ssl: bool = False
    cache_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    source_timeout_seconds: int = Field(default=5, ge=1, le=30)
    allow_manual_run: bool = False
    audience_config_path: str = "config/audiences.example.yaml"

    @model_validator(mode="after")
    def require_production_secrets(self) -> "Settings":
        if self.environment.lower() != "production":
            return self
        if not self.service_token.strip():
            raise ValueError("SERVICE_TOKEN is required in production")
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
        if not self.s3_bucket.strip() or not self.s3_region.strip():
            raise ValueError("S3_BUCKET and S3_REGION are required in production")
        if self.s3_endpoint_url and urlparse(self.s3_endpoint_url).scheme != "https":
            raise ValueError("S3_ENDPOINT_URL must use HTTPS in production")
        if not self.s3_use_ssl:
            raise ValueError("S3_USE_SSL must be enabled in production")
        if bool(self.s3_access_key_id) != bool(self.s3_secret_access_key):
            raise ValueError("S3 access key ID and secret must be configured together")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
