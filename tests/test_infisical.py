import os
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ouros_audience_engine.infisical import load_infisical_secrets
from ouros_audience_engine.settings import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_skips_infisical_when_bootstrap_is_not_configured() -> None:
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        patch("ouros_audience_engine.infisical.InfisicalSDKClient") as client,
    ):
        load_infisical_secrets()

    client.assert_not_called()


def test_rejects_partial_infisical_bootstrap() -> None:
    with (
        patch.dict(os.environ, {"INFISICAL_TOKEN": "token"}, clear=True),
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        pytest.raises(RuntimeError, match="configurados em conjunto"),
    ):
        load_infisical_secrets()


def test_rejects_unknown_infisical_environment() -> None:
    bootstrap = {
        "INFISICAL_TOKEN": "token",
        "INFISICAL_PROJECT_ID": "project",
        "INFISICAL_ENV": "staging",
        "INFISICAL_PATH": "/service",
    }
    with (
        patch.dict(os.environ, bootstrap, clear=True),
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        pytest.raises(RuntimeError, match="'prod' ou 'dev'"),
    ):
        load_infisical_secrets()


def test_production_requires_infisical_bootstrap() -> None:
    with (
        patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True),
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        pytest.raises(RuntimeError, match="INFISICAL_ENV='prod'"),
    ):
        load_infisical_secrets()


def test_production_requires_the_audience_infisical_path() -> None:
    bootstrap = {
        "ENVIRONMENT": "production",
        "INFISICAL_TOKEN": "token",
        "INFISICAL_PROJECT_ID": "project",
        "INFISICAL_ENV": "prod",
        "INFISICAL_PATH": "/wrong-service",
    }
    with (
        patch.dict(os.environ, bootstrap, clear=True),
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        pytest.raises(RuntimeError, match="INFISICAL_PATH"),
    ):
        load_infisical_secrets()


def test_production_requires_secrets_from_infisical_not_existing_environment() -> None:
    bootstrap = {
        "ENVIRONMENT": "production",
        "INFISICAL_TOKEN": "token",
        "INFISICAL_PROJECT_ID": "project",
        "INFISICAL_ENV": "prod",
        "INFISICAL_PATH": "/ms-ouros-audience-engine",
        "MONGODB_URI": "mongodb+srv://old:password@mongo.example/db",
        "SOURCE_DATABASE_URL": "postgresql://old:password@db/source?sslmode=verify-full",
        "METADATA_DATABASE_URL": "postgresql://old:password@db/meta?sslmode=verify-full",
        "REDIS_URL": "rediss://old:password@cache:6379/0",
    }
    response = SimpleNamespace(secrets=[])
    with (
        patch.dict(os.environ, bootstrap, clear=True),
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        patch("ouros_audience_engine.infisical.InfisicalSDKClient") as client,
        pytest.raises(RuntimeError, match="missing required production secrets: MONGODB_URI"),
    ):
        client.return_value.secrets.list_secrets.return_value = response
        load_infisical_secrets()


def test_infisical_cannot_override_deployment_environment() -> None:
    bootstrap = {
        "ENVIRONMENT": "production",
        "INFISICAL_TOKEN": "token",
        "INFISICAL_PROJECT_ID": "project",
        "INFISICAL_ENV": "prod",
        "INFISICAL_PATH": "/ms-ouros-audience-engine",
    }
    response = SimpleNamespace(
        secrets=[SimpleNamespace(secretKey="ENVIRONMENT", secretValue="development")]
    )
    with patch.dict(os.environ, bootstrap, clear=True):
        with (
            patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
            patch("ouros_audience_engine.infisical.InfisicalSDKClient") as client,
            pytest.raises(RuntimeError, match="ENVIRONMENT deve ser configurado no deploy"),
        ):
            client.return_value.secrets.list_secrets.return_value = response
            load_infisical_secrets()
        assert os.environ["ENVIRONMENT"] == "production"


def test_settings_use_mongodb_uri_loaded_from_infisical() -> None:
    bootstrap = {
        "ENVIRONMENT": "production",
        "INFISICAL_TOKEN": "token",
        "INFISICAL_PROJECT_ID": "project",
        "INFISICAL_ENV": "prod",
        "INFISICAL_PATH": "/ms-ouros-audience-engine",
    }
    response = SimpleNamespace(
        secrets=[
            SimpleNamespace(
                secretKey="MONGODB_URI",
                secretValue="mongodb+srv://audience:password@mongo.example/ouros_audience",
            ),
            SimpleNamespace(
                secretKey="SOURCE_DATABASE_URL",
                secretValue="postgresql://source:password@db/source?sslmode=verify-full",
            ),
            SimpleNamespace(
                secretKey="METADATA_DATABASE_URL",
                secretValue="postgresql://metadata:password@db/meta?sslmode=verify-full",
            ),
            SimpleNamespace(secretKey="REDIS_URL", secretValue="rediss://cache:6379/0"),
        ]
    )

    with (
        patch.dict(os.environ, bootstrap, clear=True),
        patch("ouros_audience_engine.infisical.load_dotenv", return_value=False),
        patch("ouros_audience_engine.infisical.InfisicalSDKClient") as client,
    ):
        client.return_value.secrets.list_secrets.return_value = response
        settings = get_settings()

    assert settings.mongodb_uri == "mongodb+srv://audience:password@mongo.example/ouros_audience"
    client.return_value.secrets.list_secrets.assert_called_once_with(
        project_id="project",
        environment_slug="prod",
        secret_path="/ms-ouros-audience-engine",
        view_secret_value=True,
    )
