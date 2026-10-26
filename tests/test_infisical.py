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


def test_settings_use_mongodb_uri_loaded_from_infisical() -> None:
    bootstrap = {
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
            )
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
