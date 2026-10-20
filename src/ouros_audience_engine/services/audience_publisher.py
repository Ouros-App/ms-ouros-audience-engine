import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_cache import BitmapReader
from ouros_audience_engine.adapters.s3_store import S3Store
from ouros_audience_engine.domain.audience import Evaluation
from ouros_audience_engine.domain.config import WaterAudienceConfig
from ouros_audience_engine.domain.manifest import SnapshotManifest, TransitionManifest
from ouros_audience_engine.domain.rules import WaterConsumptionRule
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
        s3_store: S3Store,
        codec: BitmapCodec,
        bitmap_reader: BitmapReader,
    ) -> None:
        self.source = source
        self.metadata = metadata
        self.s3_store = s3_store
        self.codec = codec
        self.bitmap_reader = bitmap_reader

    def publish(
        self,
        audience: WaterAudienceConfig,
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
            ref = SnapshotRef(audience.key, 0, None, None, None, audience.version, None, None, 0)
        previous = BitMap()
        if ref.snapshot_version > 0:
            previous = self.bitmap_reader.get_bitmap(ref)

        farm_inputs = self.source.load_water_audience_inputs(now)
        current_owner_ids = {
            owner_id for farm in farm_inputs.values() for owner_id in farm.owner_ids
        }
        self.codec.encode(current_owner_ids)
        current_members = BitMap()
        valid_farms = 0
        unknown_farms = 0
        source_watermarks = [
            period.source_watermark for farm in farm_inputs.values() for period in farm.periods
        ]
        source_watermark = max(source_watermarks) if source_watermarks else None
        rule = WaterConsumptionRule(
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
            outcome = rule.evaluate(
                farm.periods,
                was_member=was_member,
                previous_watermark=ref.source_watermark,
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

        if valid_farms == 0:
            if ref.snapshot_version > 0:
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
        unchanged = ref.snapshot_version > 0 and digest == ref.bitmap_sha256 and not rule_changed

        if unchanged:
            if dry_run:
                return PublishResult(
                    audience.key,
                    "UNCHANGED",
                    ref.snapshot_version,
                    len(current_ids),
                    unknown_farms,
                    valid_farms,
                    0,
                    0,
                    0,
                    source_watermark,
                )
            self.metadata.publish_snapshot(
                audience_key=audience.key,
                expected_version=ref.snapshot_version,
                new_version=ref.snapshot_version,
                object_key=str(ref.object_key),
                manifest_key=str(ref.manifest_key),
                bitmap_sha256=str(ref.bitmap_sha256),
                rule_version=audience.version,
                source_watermark=source_watermark,
                evaluated_at=now,
            )
            return PublishResult(
                audience.key,
                "UNCHANGED",
                ref.snapshot_version,
                len(current_ids),
                unknown_farms,
                valid_farms,
                0,
                0,
                0,
                source_watermark,
            )

        new_version = ref.snapshot_version + 1
        bootstrap = ref.snapshot_version == 0 or rule_changed
        added = self.codec.encode([] if bootstrap else added_ids)
        resolved = self.codec.encode([] if bootstrap else resolved_ids)
        revoked = self.codec.encode([] if bootstrap else revoked_ids)
        artifacts: dict[str, tuple[str, bytes]] = {}
        for kind, transition_payload in (
            ("added", added),
            ("removed_resolved", resolved),
            ("removed_access_revoked", revoked),
        ):
            transition_digest = hashlib.sha256(transition_payload).hexdigest()
            artifacts[kind] = (transition_digest, transition_payload)

        object_key = self.s3_store.snapshot_key(audience.key, new_version, digest)
        transition_keys = {
            kind: self.s3_store.transition_key(
                audience.key,
                new_version,
                "added" if kind == "added" else "removed",
                transition_digest,
            )
            for kind, (transition_digest, _) in artifacts.items()
        }
        transition_hashes = {
            kind: transition_digest for kind, (transition_digest, _) in artifacts.items()
        }
        # Removed transition objects share one S3 naming namespace; include the reason in key names.
        transition_keys["removed_resolved"] = transition_keys["removed_resolved"].replace(
            "transitions.removed-", "transitions.removed-resolved-"
        )
        transition_keys["removed_access_revoked"] = transition_keys[
            "removed_access_revoked"
        ].replace("transitions.removed-", "transitions.removed-access-revoked-")
        transition_manifest = TransitionManifest(
            audience_key=audience.key,
            snapshot_version=new_version,
            previous_version=ref.snapshot_version,
            added_key=transition_keys["added"],
            resolved_key=transition_keys["removed_resolved"],
            access_revoked_key=transition_keys["removed_access_revoked"],
            added_sha256=transition_hashes["added"],
            resolved_sha256=transition_hashes["removed_resolved"],
            access_revoked_sha256=transition_hashes["removed_access_revoked"],
            previous_object_key=ref.object_key,
            previous_sha256=ref.bitmap_sha256,
            member_id_space="farm_owner_user_id",
            rule_version=audience.version,
            evaluated_at=now,
            source_watermark=source_watermark,
        )
        transition_manifest_payload = transition_manifest.model_dump_json().encode("utf-8")
        transition_manifest_digest = hashlib.sha256(transition_manifest_payload).hexdigest()
        transition_index_key = self.s3_store.manifest_key(
            audience.key, new_version, transition_manifest_digest
        )
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
            previous_version=ref.snapshot_version,
            bootstrap=bootstrap,
            transitions={**transition_keys, "manifest": transition_index_key},
            transition_sha256=transition_hashes,
        )
        manifest_payload = manifest.model_dump_json().encode("utf-8")
        manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
        manifest_key = self.s3_store.manifest_key(audience.key, new_version, manifest_digest)

        if dry_run:
            return PublishResult(
                audience.key,
                "DRY_RUN",
                new_version,
                len(current_ids),
                unknown_farms,
                valid_farms,
                0 if bootstrap else len(added_ids),
                0 if bootstrap else len(resolved_ids),
                0 if bootstrap else len(revoked_ids),
                source_watermark,
            )

        self.metadata.ensure_audience(audience.key, audience.version)
        self.s3_store.put_immutable(object_key, payload, "application/vnd.roaringbitmap")
        for kind, (transition_digest, transition_payload) in artifacts.items():
            self.s3_store.put_immutable(
                transition_keys[kind], transition_payload, "application/vnd.roaringbitmap"
            )
            if hashlib.sha256(transition_payload).hexdigest() != transition_digest:
                raise ValueError("transition checksum changed before upload")
        self.s3_store.put_immutable(
            transition_index_key, transition_manifest_payload, "application/json"
        )
        self.s3_store.put_immutable(manifest_key, manifest_payload, "application/json")
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
            "PUBLISHED",
            new_version,
            len(current_ids),
            unknown_farms,
            valid_farms,
            0 if bootstrap else len(added_ids),
            0 if bootstrap else len(resolved_ids),
            0 if bootstrap else len(revoked_ids),
            source_watermark,
        )
