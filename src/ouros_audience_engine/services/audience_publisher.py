import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_snapshot_store import (
    RedisSnapshotStore,
    SnapshotMissing,
)
from ouros_audience_engine.domain.audience import Evaluation, RuleOutcome
from ouros_audience_engine.domain.config import (
    AudienceDefinition,
    WaterAudienceConfig,
)
from ouros_audience_engine.domain.manifest import SnapshotManifest
from ouros_audience_engine.domain.rules import TipObservationRule, WaterConsumptionRule
from ouros_audience_engine.repositories.metadata_postgres import (
    MetadataPostgresRepository,
    SnapshotRef,
)
from ouros_audience_engine.repositories.source_postgres import SourcePostgresRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublishResult:
    audience_key: str
    status: str
    snapshot_version: int
    member_count: int
    unknown_farms: int
    evaluated_farms: int
    added_count: int
    resolved_count: int
    access_revoked_count: int
    source_watermark: datetime | None


class NoValidEvaluation(RuntimeError):
    pass


class AudiencePublisher:
    def __init__(
        self,
        source: SourcePostgresRepository,
        metadata: MetadataPostgresRepository,
        snapshot_store: RedisSnapshotStore,
        codec: BitmapCodec,
    ) -> None:
        self.source = source
        self.metadata = metadata
        self.snapshot_store = snapshot_store
        self.codec = codec

    def publish(
        self,
        audience: AudienceDefinition,
        *,
        evaluated_at: datetime | None = None,
        dry_run: bool = False,
    ) -> PublishResult:
        if not audience.enabled:
            return PublishResult(audience.key, "DISABLED", 0, 0, 0, 0, 0, 0, 0, None)
        now = evaluated_at or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")

        try:
            ref = self.metadata.get_snapshot_ref(audience.key)
        except LookupError:
            ref = SnapshotRef(audience.key, 0, None, None, None, audience.version, None, None)
        previous = BitMap()
        snapshot_missing = False
        if ref.snapshot_version > 0:
            try:
                previous = self.snapshot_store.get_bitmap(ref)
            except SnapshotMissing:
                snapshot_missing = True
                logger.info("audience snapshot missing from Redis; rebuilding from source")

        current_members = BitMap()
        valid_farms = 0
        unknown_farms = 0
        source_watermarks: list[datetime] = []

        if isinstance(audience, WaterAudienceConfig):
            farm_inputs = self.source.load_water_audience_inputs(now)
            current_owner_ids = {
                owner_id for farm in farm_inputs.values() for owner_id in farm.owner_ids
            }
            source_watermarks.extend(
                period.source_watermark for farm in farm_inputs.values() for period in farm.periods
            )
            water_rule = WaterConsumptionRule(
                activation_pct=Decimal(str(audience.activation_pct)),
                resolution_pct=Decimal(str(audience.resolution_pct)),
                min_baseline_samples=audience.min_baseline_samples,
                max_observation_age_days=audience.max_observation_age_days,
            )
            for farm_id in sorted(farm_inputs):
                farm = farm_inputs[farm_id]
                if not farm.owner_ids:
                    continue
                was_member = any(owner_id in previous for owner_id in farm.owner_ids)
                outcome = water_rule.evaluate(
                    farm.periods,
                    was_member=was_member,
                    evaluated_at=now,
                )
                if outcome.evaluation is Evaluation.UNKNOWN:
                    unknown_farms += 1
                    current_members.update(
                        owner_id for owner_id in farm.owner_ids if owner_id in previous
                    )
                    continue
                valid_farms += 1
                if outcome.evaluation is Evaluation.ACTIVE:
                    current_members.update(farm.owner_ids)
                logger.info(
                    "audience farm evaluated",
                    extra={
                        "evaluation": outcome.evaluation.value,
                        "reason": outcome.reason,
                    },
                )
        else:
            owners_by_farm = self.source.load_farm_owners()
            current_owner_ids = {
                owner_id for owner_ids in owners_by_farm.values() for owner_id in owner_ids
            }
            observations = self.metadata.tip_observations(audience.tip_id)
            source_watermarks.extend(
                observation.observed_at for observation in observations.values()
            )
            tip_rule = TipObservationRule(audience)
            for farm_id in sorted(owners_by_farm):
                owner_ids = owners_by_farm[farm_id]
                if not owner_ids:
                    continue
                observation = observations.get(farm_id)
                outcome = (
                    RuleOutcome(Evaluation.UNKNOWN, None, reason="NO_OBSERVATION")
                    if observation is None
                    else tip_rule.evaluate(
                        observation.signals,
                        observed_at=observation.observed_at,
                        evaluated_at=now,
                    )
                )
                if outcome.evaluation is Evaluation.UNKNOWN:
                    unknown_farms += 1
                    current_members.update(
                        owner_id for owner_id in owner_ids if owner_id in previous
                    )
                    continue
                valid_farms += 1
                if outcome.evaluation is Evaluation.ACTIVE:
                    current_members.update(owner_ids)
                logger.info(
                    "audience farm evaluated",
                    extra={
                        "evaluation": outcome.evaluation.value,
                        "reason": outcome.reason,
                    },
                )

        self.codec.encode(current_owner_ids)
        source_watermark = max(source_watermarks) if source_watermarks else None

        if valid_farms == 0:
            if ref.snapshot_version > 0 and not snapshot_missing:
                return PublishResult(
                    audience.key,
                    "UNCHANGED_UNKNOWN",
                    ref.snapshot_version,
                    len(previous),
                    unknown_farms,
                    0,
                    0,
                    0,
                    0,
                    ref.source_watermark,
                )
            raise NoValidEvaluation(
                "no valid farm evaluations; refusing to publish an empty bootstrap"
            )

        current_ids = set(current_members)
        previous_ids = set(previous)
        resolved_ids = (previous_ids & current_owner_ids) - current_ids
        revoked_ids = previous_ids - current_owner_ids
        added_ids = current_ids - previous_ids
        payload = self.codec.encode(current_ids)
        digest = hashlib.sha256(payload).hexdigest()
        rule_changed = ref.snapshot_version > 0 and ref.rule_version != audience.version
        unchanged = (
            ref.snapshot_version > 0
            and digest == ref.bitmap_sha256
            and not rule_changed
            and not snapshot_missing
        )
        bootstrap = ref.snapshot_version == 0 or rule_changed or snapshot_missing
        new_version = (
            ref.snapshot_version
            if ref.snapshot_version > 0 and digest == ref.bitmap_sha256 and not rule_changed
            else ref.snapshot_version + 1
        )

        object_key = self.snapshot_store.snapshot_key(audience.key, new_version, digest)
        manifest = SnapshotManifest(
            audience_key=audience.key,
            snapshot_version=new_version,
            bitmap_format=self.codec.format_name,
            bitmap_format_version=self.codec.format_version,
            member_id_space=self.codec.member_id_space,
            rule_version=audience.version,
            object_key=object_key,
            member_count=len(current_ids),
            sha256=digest,
            evaluated_at=now,
            source_watermark=source_watermark,
        )
        manifest_payload = manifest.model_dump_json().encode("utf-8")
        manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
        manifest_key = self.snapshot_store.manifest_key(audience.key, new_version, manifest_digest)
        added_count = 0 if bootstrap else len(added_ids)
        resolved_count = 0 if bootstrap else len(resolved_ids)
        revoked_count = 0 if bootstrap else len(revoked_ids)

        if dry_run:
            return PublishResult(
                audience.key,
                "UNCHANGED" if unchanged else "DRY_RUN",
                new_version,
                len(current_ids),
                unknown_farms,
                valid_farms,
                0 if unchanged else added_count,
                0 if unchanged else resolved_count,
                0 if unchanged else revoked_count,
                source_watermark,
            )

        self.metadata.ensure_audience(audience.key, audience.version)
        self.snapshot_store.put_immutable(object_key, payload)
        self.snapshot_store.put_immutable(manifest_key, manifest_payload)
        self.metadata.publish_snapshot(
            audience_key=audience.key,
            expected_version=ref.snapshot_version,
            new_version=new_version,
            object_key=object_key,
            manifest_key=manifest_key,
            bitmap_sha256=digest,
            rule_version=audience.version,
            source_watermark=source_watermark,
            evaluated_at=now,
        )
        return PublishResult(
            audience.key,
            "UNCHANGED" if unchanged else "PUBLISHED",
            new_version,
            len(current_ids),
            unknown_farms,
            valid_farms,
            added_count,
            resolved_count,
            revoked_count,
            source_watermark,
        )
