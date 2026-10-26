import os

from dotenv import load_dotenv
from infisical_sdk import InfisicalSDKClient  # type: ignore[import-untyped]

PRODUCTION_SECRET_PATH = "/ms-ouros-audience-engine"
PRODUCTION_REQUIRED_SECRETS = (
    "MONGODB_URI",
    "SOURCE_DATABASE_URL",
    "METADATA_DATABASE_URL",
    "REDIS_URL",
)


def load_infisical_secrets() -> None:
    load_dotenv()
    token = os.getenv("INFISICAL_TOKEN")
    project_id = os.getenv("INFISICAL_PROJECT_ID")
    infisical_environment = os.getenv("INFISICAL_ENV")
    secret_path = os.getenv("INFISICAL_PATH")
    deployment_environment = os.getenv("ENVIRONMENT")
    is_production = (
        deployment_environment is not None
        and deployment_environment.lower() == "production"
    )

    if infisical_environment == "prod" and not is_production:
        raise RuntimeError("ENVIRONMENT deve ser 'production' quando INFISICAL_ENV='prod'")
    if is_production and infisical_environment != "prod":
        raise RuntimeError("ENVIRONMENT='production' requer INFISICAL_ENV='prod'")
    if not any((token, project_id, infisical_environment, secret_path)):
        if is_production:
            raise RuntimeError("Bootstrap do Infisical é obrigatório em produção")
        return
    if not all((token, project_id, infisical_environment, secret_path)):
        raise RuntimeError(
            "INFISICAL_TOKEN, INFISICAL_PROJECT_ID, INFISICAL_ENV e INFISICAL_PATH "
            "devem ser configurados em conjunto"
        )
    if infisical_environment not in {"prod", "dev"}:
        raise RuntimeError("INFISICAL_ENV deve ser 'prod' ou 'dev'")
    if is_production and secret_path != PRODUCTION_SECRET_PATH:
        raise RuntimeError(f"INFISICAL_PATH deve ser '{PRODUCTION_SECRET_PATH}' em produção")

    client = InfisicalSDKClient(
        host=os.getenv("INFISICAL_HOST", "https://app.infisical.com"),
        token=token,
    )
    response = client.secrets.list_secrets(
        project_id=project_id,
        environment_slug=infisical_environment,
        secret_path=secret_path,
        view_secret_value=True,
    )
    secrets = {secret.secretKey: secret.secretValue for secret in response.secrets}
    if "ENVIRONMENT" in secrets:
        raise RuntimeError("ENVIRONMENT deve ser configurado no deploy, não no Infisical")
    if is_production:
        missing = [name for name in PRODUCTION_REQUIRED_SECRETS if name not in secrets]
        if missing:
            raise RuntimeError(
                "Infisical path is missing required production secrets: " + ", ".join(missing)
            )
    for name, value in secrets.items():
        os.environ[name] = value
