import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError, PyJWKClient, decode
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError

from ouros_audience_engine.settings import Settings

bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticationKeyServiceError(RuntimeError):
    """Raised when the configured JWKS endpoint cannot provide valid keys."""


@lru_cache(maxsize=8)
def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url, cache_keys=True, lifespan=300)


def _get_signing_key(token: str, jwks_url: str) -> Any | None:
    jwks_client = _get_jwks_client(jwks_url)
    try:
        jwks_client.get_jwk_set()
    except PyJWKClientConnectionError:
        raise
    except (PyJWKClientError, PyJWKSetError, ValueError, TypeError) as exc:
        raise AuthenticationKeyServiceError("JWKS endpoint returned an invalid key set") from exc

    try:
        return jwks_client.get_signing_key_from_jwt(token)
    except PyJWKClientConnectionError:
        raise
    except PyJWKSetError as exc:
        raise AuthenticationKeyServiceError("JWKS endpoint returned an invalid key set") from exc
    except (InvalidTokenError, PyJWKClientError, ValueError):
        return None


def _decode_keycloak_token(token: str, settings: Settings) -> dict[str, Any] | None:
    issuer = settings.keycloak_issuer_url
    audience = settings.keycloak_audience
    jwks_url = settings.keycloak_jwks_url or (
        issuer.rstrip("/") + "/protocol/openid-connect/certs"
    )
    if not issuer or not audience or not jwks_url:
        return None

    signing_key = _get_signing_key(token, jwks_url)
    if signing_key is None:
        return None

    try:
        claims = decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except InvalidTokenError:
        return None
    return claims if isinstance(claims, dict) else None


def _parse_positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    user_id: int | None
    is_admin: bool


def _realm_roles(claims: dict[str, Any]) -> list[str]:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    realm_access = claims.get("realm_access")
    roles = realm_access.get("roles") if isinstance(realm_access, dict) else None
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return roles


def _farm_owner_id(claims: dict[str, Any]) -> int:
    roles = _realm_roles(claims)
    if claims.get("account_type") != "farm_owner" or "farm_owner" not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
    if _parse_positive_id(claims.get("farm_id")) is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing farm scope")
    user_id = _parse_positive_id(claims.get("database_id"))
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing database identity",
        )
    return user_id


async def _verified_claims(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = cast(Settings, request.app.state.settings)
    if not settings.keycloak_issuer_url or not settings.keycloak_audience:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Keycloak authentication is not configured",
        )

    try:
        claims = await asyncio.to_thread(_decode_keycloak_token, credentials.credentials, settings)
    except (PyJWKClientConnectionError, AuthenticationKeyServiceError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication key service unavailable",
        ) from exc
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


async def require_farm_owner_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> int:
    return _farm_owner_id(await _verified_claims(request, credentials))


async def require_admin_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> None:
    if "admin" not in _realm_roles(await _verified_claims(request, credentials)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")


async def require_farm_owner_or_admin(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> AuthenticatedCaller:
    claims = await _verified_claims(request, credentials)
    roles = _realm_roles(claims)
    if "admin" in roles:
        return AuthenticatedCaller(user_id=None, is_admin=True)
    return AuthenticatedCaller(user_id=_farm_owner_id(claims), is_admin=False)
