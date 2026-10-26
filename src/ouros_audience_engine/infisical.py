import os

from dotenv import load_dotenv
from infisical_sdk import InfisicalSDKClient  # type: ignore[import-untyped]


def load_infisical_secrets() -> None:
    load_dotenv()
    token = os.getenv("INFISICAL_TOKEN")
    project_id = os.getenv("INFISICAL_PROJECT_ID")
    environment = os.getenv("INFISICAL_ENV")
    secret_path = os.getenv("INFISICAL_PATH")
    if not any((token, project_id, environment, secret_path)):
        return
    if not all((token, project_id, environment, secret_path)):
        raise RuntimeError(
            "INFISICAL_TOKEN, INFISICAL_PROJECT_ID, INFISICAL_ENV e INFISICAL_PATH "
            "devem ser configurados em conjunto"
        )
    if environment not in {"prod", "dev"}:
        raise RuntimeError("INFISICAL_ENV deve ser 'prod' ou 'dev'")
    client = InfisicalSDKClient(
        host=os.getenv("INFISICAL_HOST", "https://app.infisical.com"),
        token=token,
    )
    response = client.secrets.list_secrets(
        project_id=project_id,
        environment_slug=environment,
        secret_path=secret_path,
        view_secret_value=True,
    )
    for secret in response.secrets:
        os.environ[secret.secretKey] = secret.secretValue
