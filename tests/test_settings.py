import pytest
from pydantic import ValidationError

from ouros_audience_engine.settings import Settings


def test_production_settings_reject_insecure_local_defaults() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL TLS"):
        Settings(_env_file=None, environment="production", service_token="secret")


def test_production_settings_accept_tls_and_iam_credentials() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        service_token="secret",
        source_database_url="postgresql://source:pw@db/source?sslmode=verify-full",
        metadata_database_url="postgresql://metadata:pw@db/meta?sslmode=verify-full",
        redis_url="rediss://cache:6379/0",
        s3_endpoint_url="",
        s3_use_ssl=True,
    )

    assert settings.environment == "production"
