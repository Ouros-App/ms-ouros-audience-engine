from dataclasses import dataclass

from ouros_audience_engine.domain.config import (
    DailyWaterMeterAuditConfig,
    TipAudienceConfig,
)
from ouros_audience_engine.domain.tip_audience_catalog import (
    daily_water_meter_audience,
    default_tip_audiences,
)
from ouros_audience_engine.repositories.audience_definitions_mongo import (
    AudienceDefinitionsMongoRepository,
)
from ouros_audience_engine.repositories.metadata_postgres import MetadataPostgresRepository


class TipAudienceSeedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TipAudienceSeedResult:
    created_count: int
    linked_count: int


def seed_tip_audiences(
    definitions: AudienceDefinitionsMongoRepository,
    metadata: MetadataPostgresRepository,
) -> TipAudienceSeedResult:
    catalog: tuple[TipAudienceConfig | DailyWaterMeterAuditConfig, ...] = (
        *default_tip_audiences(),
        daily_water_meter_audience(),
    )
    expected_tip_ids = {item.tip_id for item in catalog}
    existing_tip_ids = {tip.id for tip in metadata.list_tips(search=None, limit=200, offset=0)}
    missing_tip_ids = expected_tip_ids - existing_tip_ids
    if missing_tip_ids:
        raise TipAudienceSeedError(
            f"production tips are missing expected IDs: {sorted(missing_tip_ids)}"
        )

    keys = [item.key for item in catalog]
    current_links = metadata.tip_links_for_audiences(keys)
    current_definitions = {item.key: definitions.get(item.key) for item in catalog}
    for item in catalog:
        existing = current_definitions[item.key]
        if existing is not None and existing != item:
            raise TipAudienceSeedError(f"refusing to overwrite existing audience: {item.key}")
        if any(link.tip_id != item.tip_id for link in current_links.get(item.key, [])):
            raise TipAudienceSeedError(f"audience has a mismatched tip link: {item.key}")

    created_count = 0
    linked_count = 0
    for item in catalog:
        if current_definitions[item.key] is None:
            definitions.create(item)
            created_count += 1
        if not current_links.get(item.key):
            if not metadata.link_tip(
                item.key,
                item.tip_id,
                item.version,
                enabled=item.enabled,
            ):
                raise TipAudienceSeedError(f"failed to link audience to tip: {item.key}")
            linked_count += 1
    return TipAudienceSeedResult(created_count, linked_count)
