import pytest
from pydantic import ValidationError

from ouros_audience_engine.settings import Settings


def test_production_settings_reject_insecure_local_defaults() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL TLS"):
        Settings(_env_file=None, environment="production")


def test_production_rejects_non_https_keycloak_issuer() -> None:
    with pytest.raises(ValidationError, match="KEYCLOAK_ISSUER_URL"):
        Settings(
            _env_file=None,
            environment="production",
            keycloak_issuer_url="http://issuer.example/realms/ouros",
        )


def test_production_settings_accept_tls_connections() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        source_database_url="postgresql://source:pw@db/source?sslmode=verify-full",
        metadata_database_url="postgresql://metadata:pw@db/meta?sslmode=verify-full",
        redis_url="rediss://cache:6379/0",
        mongodb_uri="mongodb+srv://audience:pw@mongo.example",
    )


    assert settings.environment == "production"


def test_production_rejects_mongodb_without_tls() -> None:
    with pytest.raises(ValidationError, match="MONGODB_URI must use TLS"):
        Settings(
            _env_file=None,
            environment="production",
            source_database_url="postgresql://source:pw@db/source?sslmode=verify-full",
            metadata_database_url="postgresql://metadata:pw@db/meta?sslmode=verify-full",
            redis_url="rediss://cache:6379/0",
            mongodb_uri="mongodb://audience:pw@mongo.example",
        )
