import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pyroaring import BitMap

from ouros_audience_engine.adapters.redis_cache import SnapshotUnavailable
from ouros_audience_engine.api.schemas import ResolveRequest, ResolveResponse
from ouros_audience_engine.domain.config import AudienceConfig
from ouros_audience_engine.repositories.metadata_postgres import (
    MetadataPostgresRepository,
    SnapshotRef,
)

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


class BitmapReaderProtocol(Protocol):
    def get_bitmap(self, snapshot: SnapshotRef) -> BitMap: ...


@dataclass(frozen=True, slots=True)
class ApiServices:
    metadata: MetadataPostgresRepository
    bitmap_reader: BitmapReaderProtocol
    config: AudienceConfig
    service_token: str


router = APIRouter(prefix="/internal/v1", tags=["internal"])


def get_api_services(request: Request) -> ApiServices:
    return cast(ApiServices, request.app.state.api_services)


def require_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    services: ApiServices = Depends(get_api_services),
) -> None:
    if not services.service_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service authentication is not configured",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="service authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(credentials.credentials, services.service_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.post(
    "/tips/resolve",
    response_model=ResolveResponse,
    dependencies=[Depends(require_service_token)],
)
def resolve_tips(
    request: ResolveRequest,
    services: ApiServices = Depends(get_api_services),
) -> ResolveResponse:
    audiences = [audience for audience in services.config.audiences if audience.enabled]
    audience_keys = [audience.key for audience in audiences]
    try:
        tip_ids_by_audience = services.metadata.enabled_tip_ids(audience_keys)
        audience_versions: dict[str, int] = {}
        tip_ids: set[int] = set()
        now = datetime.now(UTC)
        evaluated_at = now
        source_status = "CURRENT"
        for audience in audiences:
            linked_tip_ids = tip_ids_by_audience.get(audience.key, [])
            if not linked_tip_ids:
                continue
            snapshot = services.metadata.get_snapshot_ref(audience.key)
            if snapshot.snapshot_version < 1:
                raise SnapshotUnavailable("enabled audience has no published snapshot")
            bitmap = services.bitmap_reader.get_bitmap(snapshot)
            audience_versions[audience.key] = snapshot.snapshot_version
            if snapshot.evaluated_at is not None:
                evaluated_at = min(evaluated_at, snapshot.evaluated_at)
            if snapshot.source_watermark is None or now - snapshot.source_watermark > timedelta(
                days=audience.max_observation_age_days
            ):
                source_status = "STALE"
            if request.user_id in bitmap:
                tip_ids.update(linked_tip_ids)
        return ResolveResponse(
            user_id=request.user_id,
            tip_ids=sorted(tip_ids),
            evaluated_at=evaluated_at,
            snapshot_info=audience_versions,
            source_status=source_status,
        )
    except SnapshotUnavailable as exc:
        logger.warning("membership resolution failed closed: invalid audience snapshot")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audience membership is temporarily unavailable",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("membership resolution failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audience membership is temporarily unavailable",
        ) from exc
