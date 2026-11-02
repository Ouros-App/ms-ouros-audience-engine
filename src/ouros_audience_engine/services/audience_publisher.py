import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event as ThreadEvent
from typing import Literal, overload

from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_snapshot_store import (
    ImmutableSnapshotConflict,
    RedisSnapshotStore,
    SnapshotMissing,
)
from ouros_audience_engine.domain.audience import Evaluation, RuleOutcome
from ouros_audience_engine.domain.config import (
    AudienceDefinition,
    DailyWaterMeterAuditConfig,
    WaterAudienceConfig,
)
from ouros_audience_engine.domain.manifest import SnapshotManifest
from ouros_audience_engine.domain.rules import (
    DailyWaterMeterAuditRule,
    TipObservationRule,
    WaterConsumptionRule,
)
from ouros_audience_engine.repositories.metadata_postgres import (
    ConcurrentPublishError,
    MetadataPostgresRepository,
    SnapshotRef,
)
from ouros_audience_engine.repositories.source_postgres import SourcePostgresRepository

logger = logging.getLogger(__name__)


def _raise_if_stopping(stop_event: ThreadEvent | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("audience publication stopped for shutdown")


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



@dataclass(frozen=True, slots=True)
class PreparedPublication:
    audience_key: str
    expected_snapshot_version: int
    snapshot_version: int
    object_key: str | None
    manifest_key: str | None
    bitmap_sha256: str | None
    rule_version: str
    source_watermark: datetime | None
    evaluated_at: datetime | None
    member_ids: tuple[int, ...]
    result: PublishResult
    commit: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "audience_key": self.audience_key,
            "expected_snapshot_version": self.expected_snapshot_version,
            "snapshot_version": self.snapshot_version,
            "object_key": self.object_key,
            "manifest_key": self.manifest_key,
            "bitmap_sha256": self.bitmap_sha256,
            "rule_version": self.rule_version,
            "source_watermark": (
                self.source_watermark.isoformat() if self.source_watermark else None
            ),
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "member_ids": list(self.member_ids),
            "result": {
                "audience_key": self.result.audience_key,
                "status": self.result.status,
                "snapshot_version": self.result.snapshot_version,
                "member_count": self.result.member_count,
                "unknown_farms": self.result.unknown_farms,
                "evaluated_farms": self.result.evaluated_farms,
                "added_count": self.result.added_count,
                "resolved_count": self.result.resolved_count,
                "access_revoked_count": self.result.access_revoked_count,
                "source_watermark": (
                    self.result.source_watermark.isoformat()
                    if self.result.source_watermark
                    else None
                ),
            },
            "commit": self.commit,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "PreparedPublication":
        raw_result = value["result"]
        raw_member_ids = value["member_ids"]
        raw_commit = value["commit"]
        expected_version = value["expected_snapshot_version"]
        snapshot_version = value["snapshot_version"]
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or isinstance(snapshot_version, bool)
            or not isinstance(snapshot_version, int)
        ):
            raise ValueError("invalid prepared audience snapshot version")
        if (
            not isinstance(raw_result, dict)
            or not isinstance(raw_member_ids, list)
            or not isinstance(raw_commit, bool)
        ):
            raise ValueError("invalid prepared audience publication record")
        result_watermark = raw_result["source_watermark"]
        result = PublishResult(
            audience_key=str(raw_result["audience_key"]),
            status=str(raw_result["status"]),
            snapshot_version=int(raw_result["snapshot_version"]),
            member_count=int(raw_result["member_count"]),
            unknown_farms=int(raw_result["unknown_farms"]),
            evaluated_farms=int(raw_result["evaluated_farms"]),
            added_count=int(raw_result["added_count"]),
            resolved_count=int(raw_result["resolved_count"]),
            access_revoked_count=int(raw_result["access_revoked_count"]),
            source_watermark=(
                datetime.fromisoformat(str(result_watermark)) if result_watermark else None
            ),
        )
        source_watermark = value["source_watermark"]
        evaluated_at = value["evaluated_at"]
        return cls(
            audience_key=str(value["audience_key"]),
            expected_snapshot_version=expected_version,
            snapshot_version=snapshot_version,
            object_key=str(value["object_key"]) if value["object_key"] is not None else None,
            manifest_key=str(value["manifest_key"]) if value["manifest_key"] is not None else None,
            bitmap_sha256=(
                str(value["bitmap_sha256"]) if value["bitmap_sha256"] is not None else None
            ),
            rule_version=str(value["rule_version"]),
            source_watermark=(
                datetime.fromisoformat(str(source_watermark)) if source_watermark else None
            ),
            evaluated_at=datetime.fromisoformat(str(evaluated_at)) if evaluated_at else None,
            member_ids=tuple(int(member_id) for member_id in raw_member_ids),
            result=result,
            commit=raw_commit,
        )

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

    @overload
    def publish(
        self,
        audience: AudienceDefinition,
        *,
        evaluated_at: datetime | None = None,
        dry_run: bool = False,
        stop_event: ThreadEvent | None = None,
        prepare_only: Literal[True],
    ) -> PreparedPublication: ...

    @overload
    def publish(
        self,
        audience: AudienceDefinition,
        *,
        evaluated_at: datetime | None = None,
        dry_run: bool = False,
        stop_event: ThreadEvent | None = None,
        prepare_only: Literal[False] = False,
    ) -> PublishResult: ...

    def publish(
        self,
        audience: AudienceDefinition,
        *,
        evaluated_at: datetime | None = None,
        dry_run: bool = False,
        stop_event: ThreadEvent | None = None,
        prepare_only: bool = False,
    ) -> PublishResult | PreparedPublication:
        if not audience.enabled:
            return PublishResult(audience.key, "DISABLED", 0, 0, 0, 0, 0, 0, 0, None)
        _raise_if_stopping(stop_event)
        now = evaluated_at or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")

        try:
            ref = self.metadata.get_snapshot_ref(audience.key)
        except LookupError:
            ref = SnapshotRef(audience.key, 0, None, None, None, audience.version, None, None)
        rule_changed = ref.snapshot_version > 0 and ref.rule_version != audience.version
        _raise_if_stopping(stop_event)
        previous = BitMap()
        snapshot_missing = False
        if ref.snapshot_version > 0:
            try:
                previous = self.snapshot_store.get_bitmap(ref)
            except SnapshotMissing:
                snapshot_missing = True
                logger.info("audience snapshot missing from Redis; rebuilding from source")

        _raise_if_stopping(stop_event)
        current_members = BitMap()
        valid_farms = 0
        unknown_farms = 0
        source_watermarks: list[datetime] = []

        if isinstance(audience, DailyWaterMeterAuditConfig):
            owners_by_farm = self.source.load_farm_owners()
            _raise_if_stopping(stop_event)
            current_owner_ids = {
                owner_id for owner_ids in owners_by_farm.values() for owner_id in owner_ids
            }
            meter_inputs = self.source.load_daily_water_meter_audit_inputs()
            _raise_if_stopping(stop_event)
            if (
                evaluated_at is None
                and meter_inputs.source_watermark is not None
                and meter_inputs.source_watermark.tzinfo is not None
            ):
                now = max(now, meter_inputs.source_watermark.astimezone(UTC))
            meter_rule = DailyWaterMeterAuditRule(audience.max_reading_age_hours)
            for farm_id, owner_ids in sorted(owners_by_farm.items()):
                _raise_if_stopping(stop_event)
                if not owner_ids:
                    continue
                if (
                    farm_id not in meter_inputs.latest_reading_by_farm
                    or meter_inputs.source_watermark is None
                ):
                    outcome = RuleOutcome(Evaluation.UNKNOWN, None, reason="MISSING_SOURCE_DATA")
                else:
                    outcome = meter_rule.evaluate(
                        meter_inputs.latest_reading_by_farm[farm_id],
                        source_watermark=meter_inputs.source_watermark,
                        evaluated_at=now,
                    )
                if outcome.evaluation is Evaluation.UNKNOWN:
                    unknown_farms += 1
                    if not rule_changed:
                        current_members.update(
                            owner_id for owner_id in owner_ids if owner_id in previous
                        )
                    continue
                if outcome.source_watermark is not None:
                    source_watermarks.append(outcome.source_watermark)
                valid_farms += 1
                if outcome.evaluation is Evaluation.ACTIVE:
                    current_members.update(owner_ids)
        elif isinstance(audience, WaterAudienceConfig):
            farm_inputs = self.source.load_water_audience_inputs(now)
            _raise_if_stopping(stop_event)
            current_owner_ids = {
                owner_id for farm in farm_inputs.values() for owner_id in farm.owner_ids
            }
            water_rule = WaterConsumptionRule(
                activation_pct=Decimal(str(audience.activation_pct)),
                resolution_pct=Decimal(str(audience.resolution_pct)),
                min_baseline_samples=audience.min_baseline_samples,
                max_observation_age_days=audience.max_observation_age_days,
            )
            for farm_id in sorted(farm_inputs):
                _raise_if_stopping(stop_event)
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
                    if not rule_changed:
                        current_members.update(
                            owner_id for owner_id in farm.owner_ids if owner_id in previous
                        )
                    continue
                if outcome.source_watermark is not None:
                    source_watermarks.append(outcome.source_watermark)
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
            _raise_if_stopping(stop_event)
            current_owner_ids = {
                owner_id for owner_ids in owners_by_farm.values() for owner_id in owner_ids
            }
            observations = self.metadata.tip_observations(audience.tip_id)
            _raise_if_stopping(stop_event)
            tip_rule = TipObservationRule(audience)
            for farm_id in sorted(owners_by_farm):
                _raise_if_stopping(stop_event)
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
                    if not rule_changed:
                        current_members.update(
                            owner_id for owner_id in owner_ids if owner_id in previous
                        )
                    continue
                if outcome.source_watermark is not None:
                    source_watermarks.append(outcome.source_watermark)
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

        _raise_if_stopping(stop_event)
        self.codec.encode(current_owner_ids)
        _raise_if_stopping(stop_event)
        source_watermark = (
            min(source_watermarks)
            if valid_farms > 0 and unknown_farms == 0 and source_watermarks
            else None
        )

        if valid_farms == 0 and (ref.snapshot_version == 0 or snapshot_missing or rule_changed):
            raise NoValidEvaluation(
                "no valid farm evaluations; refusing to publish an empty or stale-rule snapshot"
            )

        current_ids = set(current_members)
        previous_ids = set(previous)
        resolved_ids = (previous_ids & current_owner_ids) - current_ids
        revoked_ids = previous_ids - current_owner_ids
        added_ids = current_ids - previous_ids
        payload = self.codec.encode(current_ids)
        digest = hashlib.sha256(payload).hexdigest()
        unchanged = (
            ref.snapshot_version > 0
            and digest == ref.bitmap_sha256
            and not rule_changed
            and not snapshot_missing
        )
        unchanged_unknown = unchanged and valid_farms == 0 and unknown_farms > 0
        bootstrap = ref.snapshot_version == 0 or rule_changed or snapshot_missing
        new_version = (
            ref.snapshot_version
            if ref.snapshot_version > 0 and digest == ref.bitmap_sha256 and not rule_changed
            else ref.snapshot_version + 1
        )
        if unchanged_unknown:
            status = "UNCHANGED_UNKNOWN"
        elif unchanged:
            status = "UNCHANGED"
        else:
            status = "DRY_RUN" if dry_run else "PUBLISHED"
        _raise_if_stopping(stop_event)
        if unchanged_unknown and ref.source_watermark is None:
            unchanged_result = PublishResult(
                audience.key,
                status,
                ref.snapshot_version,
                len(current_ids),
                unknown_farms,
                valid_farms,
                0,
                0,
                0,
                None,
            )
            if prepare_only:
                return PreparedPublication(
                    audience.key,
                    ref.snapshot_version,
                    ref.snapshot_version,
                    ref.object_key,
                    ref.manifest_key,
                    ref.bitmap_sha256,
                    audience.version,
                    ref.source_watermark,
                    ref.evaluated_at,
                    tuple(sorted(current_ids)),
                    unchanged_result,
                    False,
                )
            return unchanged_result

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

        _raise_if_stopping(stop_event)
        result = PublishResult(
            audience.key,
            status,
            new_version,
            len(current_ids),
            unknown_farms,
            valid_farms,
            0 if unchanged else added_count,
            0 if unchanged else resolved_count,
            0 if unchanged else revoked_count,
            source_watermark,
        )
        if dry_run:
            return result

        _raise_if_stopping(stop_event)
        self.metadata.ensure_audience(audience.key, audience.version)
        self.snapshot_store.put_immutable(object_key, payload)
        self.snapshot_store.put_immutable(manifest_key, manifest_payload)
        if prepare_only:
            return PreparedPublication(
                audience.key,
                ref.snapshot_version,
                new_version,
                object_key,
                manifest_key,
                digest,
                audience.version,
                source_watermark,
                now,
                tuple(sorted(current_ids)),
                result,
                True,
            )
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
        return result

    def publish_prepared(self, prepared: PreparedPublication) -> PublishResult:
        try:
            current = self.metadata.get_snapshot_ref(prepared.audience_key)
        except LookupError:
            current = SnapshotRef(
                prepared.audience_key,
                0,
                None,
                None,
                None,
                prepared.rule_version,
                None,
                None,
            )
        if not prepared.commit:
            if (
                current.snapshot_version != prepared.snapshot_version
                or current.object_key != prepared.object_key
                or current.manifest_key != prepared.manifest_key
                or current.rule_version != prepared.rule_version
                or current.bitmap_sha256 != prepared.bitmap_sha256
            ):
                raise ConcurrentPublishError(
                    f"snapshot pointer changed: {prepared.audience_key}"
                )
            return prepared.result
        object_key = prepared.object_key
        manifest_key = prepared.manifest_key
        bitmap_sha256 = prepared.bitmap_sha256
        evaluated_at = prepared.evaluated_at
        if (
            object_key is None
            or manifest_key is None
            or bitmap_sha256 is None
            or evaluated_at is None
        ):
            raise ValueError("committed prepared publication is missing snapshot metadata")
        candidate_ref = SnapshotRef(
            prepared.audience_key,
            prepared.snapshot_version,
            object_key,
            manifest_key,
            bitmap_sha256,
            prepared.rule_version,
            prepared.source_watermark,
            evaluated_at,
        )
        candidate_members = self.snapshot_store.get_bitmap(candidate_ref)
        if tuple(candidate_members) != prepared.member_ids:
            raise ImmutableSnapshotConflict(
                f"prepared snapshot bytes changed: {prepared.audience_key}"
            )
        if (
            current.snapshot_version == prepared.snapshot_version
            and current.object_key == prepared.object_key
            and current.manifest_key == prepared.manifest_key
            and current.bitmap_sha256 == prepared.bitmap_sha256
            and current.rule_version == prepared.rule_version
        ):
            return prepared.result
        if current.snapshot_version != prepared.expected_snapshot_version:
            raise ConcurrentPublishError(f"snapshot pointer changed: {prepared.audience_key}")
        self.metadata.publish_snapshot(
            audience_key=prepared.audience_key,
            expected_version=prepared.expected_snapshot_version,
            new_version=prepared.snapshot_version,
            object_key=object_key,
            manifest_key=manifest_key,
            bitmap_sha256=bitmap_sha256,
            rule_version=prepared.rule_version,
            source_watermark=prepared.source_watermark,
            evaluated_at=evaluated_at,
        )
        return prepared.result
