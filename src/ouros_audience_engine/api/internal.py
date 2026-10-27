import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pyroaring import BitMap

from ouros_audience_engine.adapters.redis_snapshot_store import (
    SnapshotMissing,
    SnapshotUnavailable,
)
from ouros_audience_engine.api.schemas import (
    ResolveRequest,
    ResolveResponse,
    TipObservationWriteRequest,
    UserAudienceEntry,
    UserAudiencesResponse,
)
from ouros_audience_engine.api.security import (
    AuthenticatedCaller,
    require_admin_token,
    require_farm_owner_or_admin,
    require_farm_owner_token,
)
from ouros_audience_engine.domain.config import (
    AudienceConfig,
    AudienceDefinition,
    TipAudienceConfig,
)
from ouros_audience_engine.repositories.audience_definitions_mongo import (
    AudienceDefinitionConflict,
    AudienceDefinitionsMongoRepository,
)
from ouros_audience_engine.repositories.metadata_postgres import (
    MetadataPostgresRepository,
    SnapshotRef,
    TipAudienceLink,
)

logger = logging.getLogger(__name__)


class SnapshotStoreProtocol(Protocol):
    def get_bitmap(self, snapshot: SnapshotRef) -> BitMap: ...
    def delete_audience(self, audience_key: str) -> None: ...


class PublisherProtocol(Protocol):
    def publish(self, audience: AudienceDefinition) -> object: ...


@dataclass(slots=True)
class ApiServices:
    metadata: MetadataPostgresRepository
    snapshot_store: SnapshotStoreProtocol
    publisher: PublisherProtocol
    config: AudienceConfig
    definitions: AudienceDefinitionsMongoRepository | None = None


router = APIRouter(prefix="/internal/v1", tags=["internal"])


def get_api_services(request: Request) -> ApiServices:
    return cast(ApiServices, request.app.state.api_services)


def _load_audience_snapshots(
    audiences: list[AudienceDefinition],
    services: ApiServices,
) -> dict[str, tuple[SnapshotRef, BitMap]]:
    for attempt in range(2):
        snapshots: dict[str, tuple[SnapshotRef, BitMap]] = {}
        missing: list[AudienceDefinition] = []
        for audience in audiences:
            try:
                snapshot = services.metadata.get_snapshot_ref(audience.key)
            except LookupError:
                missing.append(audience)
                continue
            if snapshot.snapshot_version < 1 or snapshot.rule_version != audience.version:
                missing.append(audience)
                continue
            try:
                bitmap = services.snapshot_store.get_bitmap(snapshot)
            except SnapshotMissing:
                missing.append(audience)
                continue
            snapshots[audience.key] = (snapshot, bitmap)

        if not missing:
            return snapshots
        if attempt == 1:
            raise SnapshotUnavailable("audience snapshot remains missing after rebuild")

        with services.metadata.publish_job_lock():
            for audience in missing:
                try:
                    current = services.metadata.get_snapshot_ref(audience.key)
                except LookupError:
                    services.publisher.publish(audience)
                    continue
                if current.snapshot_version > 0 and current.rule_version == audience.version:
                    try:
                        services.snapshot_store.get_bitmap(current)
                    except SnapshotMissing:
                        pass
                    else:
                        continue
                services.publisher.publish(audience)
    raise SnapshotUnavailable("audience snapshot remains missing after rebuild")


def _require_definitions(services: ApiServices) -> AudienceDefinitionsMongoRepository:
    if services.definitions is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audience definitions are unavailable",
        )
    return services.definitions


def _admin_audience(
    definition: AudienceDefinition,
    links: list[TipAudienceLink],
) -> dict[str, object]:
    return {
        "definition": definition.model_dump(),
        "tips": [{"tip_id": link.tip_id, "enabled": link.enabled} for link in links],
    }


@router.get("/admin/audiences")
def list_admin_audiences(
    search: str | None = Query(default=None, max_length=100),
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> list[dict[str, object]]:
    definitions = _require_definitions(services)
    try:
        items = definitions.list(search)
        links = services.metadata.tip_links_for_audiences([item.key for item in items])
        return [_admin_audience(item, links.get(item.key, [])) for item in items]
    except Exception as exc:
        logger.exception("audience administration list failed")
        raise HTTPException(status_code=503, detail="audience administration unavailable") from exc


@router.get("/admin/audiences/{key}")
def get_admin_audience(
    key: str,
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> dict[str, object]:
    definitions = _require_definitions(services)
    try:
        definition = definitions.get(key)
        if definition is None:
            raise HTTPException(status_code=404, detail="audience not found")
        links = services.metadata.tip_links_for_audiences([key])
        return _admin_audience(definition, links.get(key, []))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("audience administration read failed")
        raise HTTPException(status_code=503, detail="audience administration unavailable") from exc


@router.post("/admin/audiences", status_code=status.HTTP_201_CREATED)
def create_admin_audience(
    definition: AudienceDefinition,
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> dict[str, object]:
    repository = _require_definitions(services)
    try:
        repository.create(definition)
        services.config = repository.load()
        return _admin_audience(definition, [])
    except AudienceDefinitionConflict as exc:
        raise HTTPException(status_code=409, detail="audience already exists") from exc
    except Exception as exc:
        logger.exception("audience administration create failed")
        raise HTTPException(status_code=503, detail="audience administration unavailable") from exc


@router.put("/admin/audiences/{key}")
def replace_admin_audience(
    key: str,
    definition: AudienceDefinition,
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> dict[str, object]:
    repository = _require_definitions(services)
    if key != definition.key:
        raise HTTPException(status_code=422, detail="path key must match definition key")
    try:
        current = repository.get(key)
        if current is None:
            raise HTTPException(status_code=404, detail="audience not found")
        if (
            current.model_dump(exclude={"version"}) != definition.model_dump(exclude={"version"})
            and current.version == definition.version
        ):
            raise HTTPException(
                status_code=409,
                detail="changing audience rules requires a new version",
            )
        if not repository.replace(key, definition):
            raise HTTPException(status_code=404, detail="audience not found")
        services.config = repository.load()
        links = services.metadata.tip_links_for_audiences([key])
        return _admin_audience(definition, links.get(key, []))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("audience administration update failed")
        raise HTTPException(status_code=503, detail="audience administration unavailable") from exc


@router.delete("/admin/audiences/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_admin_audience(
    key: str,
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> None:
    repository = _require_definitions(services)
    try:
        if repository.get(key) is None:
            raise HTTPException(status_code=404, detail="audience not found")
        services.snapshot_store.delete_audience(key)
        services.metadata.delete_audience(key)
        repository.delete(key)
        services.config = repository.load()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("audience administration delete failed")
        raise HTTPException(status_code=503, detail="audience administration unavailable") from exc


@router.get("/admin/tips")
def search_admin_tips(
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> list[dict[str, object]]:
    try:
        return [
            {"id": tip.id, "tip": tip.tip}
            for tip in services.metadata.list_tips(search, limit, offset)
        ]
    except Exception as exc:
        logger.exception("tip administration search failed")
        raise HTTPException(status_code=503, detail="tip administration unavailable") from exc


@router.put(
    "/admin/farms/{farm_id}/tips/{tip_id}/observation",
    status_code=status.HTTP_204_NO_CONTENT,
)
def upsert_admin_tip_observation(
    payload: TipObservationWriteRequest,
    farm_id: int = Path(gt=0, le=2_147_483_647),
    tip_id: int = Path(gt=0, le=2_147_483_647),
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> None:
    audience = next(
        (
            item
            for item in services.config.audiences
            if isinstance(item, TipAudienceConfig) and item.tip_id == tip_id
        ),
        None,
    )
    if audience is None:
        raise HTTPException(status_code=404, detail="tip audience not found")
    if set(payload.signals) != set(audience.required_signals):
        raise HTTPException(
            status_code=422,
            detail="signals must exactly match the configured fields",
        )
    if any(
        isinstance(payload.signals[field], bool) != expects_boolean
        for field, expects_boolean in audience.required_signals.items()
    ):
        raise HTTPException(status_code=422, detail="signal value types do not match the rule")
    try:
        saved = services.metadata.upsert_tip_observation(
            farm_id,
            tip_id,
            payload.observed_at,
            payload.signals,
        )
        if not saved:
            raise HTTPException(status_code=404, detail="farm or tip not found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("tip observation write failed")
        raise HTTPException(status_code=503, detail="tip observation unavailable") from exc


@router.put(
    "/admin/audiences/{key}/tips/{tip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def link_admin_tip(
    key: str,
    tip_id: int = Path(gt=0),
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> None:
    repository = _require_definitions(services)
    try:
        definition = repository.get(key)
        if definition is None:
            raise HTTPException(status_code=404, detail="audience not found")
        if isinstance(definition, TipAudienceConfig) and definition.tip_id != tip_id:
            raise HTTPException(status_code=409, detail="tip ID does not match audience rule")
        enabled = definition.enabled if isinstance(definition, TipAudienceConfig) else True
        if not services.metadata.link_tip(key, tip_id, definition.version, enabled=enabled):
            raise HTTPException(status_code=404, detail="tip not found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("tip association failed")
        raise HTTPException(status_code=503, detail="tip administration unavailable") from exc


@router.delete(
    "/admin/audiences/{key}/tips/{tip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def unlink_admin_tip(
    key: str,
    tip_id: int = Path(gt=0),
    services: ApiServices = Depends(get_api_services),
    _admin: None = Depends(require_admin_token),
) -> None:
    repository = _require_definitions(services)
    try:
        if repository.get(key) is None:
            raise HTTPException(status_code=404, detail="audience not found")
        services.metadata.unlink_tip(key, tip_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("tip association removal failed")
        raise HTTPException(status_code=503, detail="tip administration unavailable") from exc


@router.get("/users/{user_id}/audiences", response_model=UserAudiencesResponse)
def user_audiences(
    user_id: int = Path(gt=0, le=2_147_483_647),
    services: ApiServices = Depends(get_api_services),
    caller: AuthenticatedCaller = Depends(require_farm_owner_or_admin),
) -> UserAudiencesResponse:
    if not caller.is_admin and caller.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requested user does not match authenticated identity",
        )

    audiences = [audience for audience in services.config.audiences if audience.enabled]
    audience_keys = [audience.key for audience in audiences]
    try:
        snapshots = _load_audience_snapshots(audiences, services)
        tip_ids_by_audience = services.metadata.enabled_tip_ids(audience_keys)
        now = datetime.now(UTC)
        source_status = "CURRENT"
        memberships: list[UserAudienceEntry] = []
        for audience in audiences:
            snapshot, bitmap = snapshots[audience.key]
            if snapshot.source_watermark is None or now - snapshot.source_watermark > timedelta(
                days=audience.max_observation_age_days
            ):
                source_status = "STALE"
            if user_id in bitmap:
                memberships.append(
                    UserAudienceEntry(
                        key=audience.key,
                        version=audience.version,
                        tip_ids=tip_ids_by_audience.get(audience.key, []),
                        snapshot_version=snapshot.snapshot_version,
                    )
                )
        return UserAudiencesResponse(
            user_id=user_id,
            audiences=memberships,
            source_status=source_status,
        )
    except SnapshotUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audience membership is temporarily unavailable",
        ) from exc
    except Exception as exc:
        logger.exception("audience membership lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audience membership is temporarily unavailable",
        ) from exc


@router.post(
    "/tips/resolve",
    response_model=ResolveResponse,
)
def resolve_tips(
    request: ResolveRequest,
    services: ApiServices = Depends(get_api_services),
    authenticated_user_id: int = Depends(require_farm_owner_token),
) -> ResolveResponse:
    if request.user_id != authenticated_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requested user does not match authenticated identity",
        )
    audiences = [audience for audience in services.config.audiences if audience.enabled]
    audience_keys = [audience.key for audience in audiences]
    try:
        tip_ids_by_audience = services.metadata.enabled_tip_ids(audience_keys)
        linked_audiences = [
            audience for audience in audiences if tip_ids_by_audience.get(audience.key)
        ]
        snapshots = _load_audience_snapshots(linked_audiences, services)
        now = datetime.now(UTC)
        audience_versions: dict[str, int] = {}
        tip_ids: set[int] = set()
        evaluated_at = now
        source_status = "CURRENT"
        for audience in linked_audiences:
            snapshot, bitmap = snapshots[audience.key]
            audience_versions[audience.key] = snapshot.snapshot_version
            if snapshot.evaluated_at is not None:
                evaluated_at = min(evaluated_at, snapshot.evaluated_at)
            if snapshot.source_watermark is None or now - snapshot.source_watermark > timedelta(
                days=audience.max_observation_age_days
            ):
                source_status = "STALE"
            if request.user_id in bitmap:
                tip_ids.update(tip_ids_by_audience[audience.key])
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
