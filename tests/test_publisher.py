from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_snapshot_store import SnapshotMissing
from ouros_audience_engine.domain.audience import MeasurementPeriod
from ouros_audience_engine.domain.config import WaterAudienceConfig
from ouros_audience_engine.domain.tip_audience_catalog import default_tip_audiences
from ouros_audience_engine.repositories.metadata_postgres import SnapshotRef, TipObservation
from ouros_audience_engine.repositories.source_postgres import FarmData
from ouros_audience_engine.services.audience_publisher import AudiencePublisher, NoValidEvaluation


class MemorySource:
    def __init__(
        self,
        farms: dict[int, FarmData],
        owner_ids: dict[int, set[int]] | None = None,
    ) -> None:
        self.farms = farms
        self.owner_ids = owner_ids or {farm_id: farm.owner_ids for farm_id, farm in farms.items()}

    def load_farm_owners(self) -> dict[int, set[int]]:
        return self.owner_ids

    def load_water_audience_inputs(self, evaluated_at: datetime) -> dict[int, FarmData]:
        return self.farms


class MemoryMetadata:
    def __init__(self) -> None:
        self.refs: dict[str, SnapshotRef] = {}
        self.observations: dict[int, TipObservation] = {}

    def get_snapshot_ref(self, audience_key: str) -> SnapshotRef:
        try:
            return self.refs[audience_key]
        except KeyError as exc:
            raise LookupError(audience_key) from exc

    def tip_observations(self, tip_id: int) -> dict[int, TipObservation]:
        return {
            farm_id: observation
            for farm_id, observation in self.observations.items()
            if observation.tip_id == tip_id
        }

    def ensure_audience(self, audience_key: str, rule_version: str) -> None:
        self.refs.setdefault(
            audience_key,
            SnapshotRef(audience_key, 0, None, None, None, rule_version, None, None),
        )

    def publish_snapshot(
        self,
        *,
        audience_key: str,
        expected_version: int,
        new_version: int,
        object_key: str,
        manifest_key: str,
        bitmap_sha256: str,
        rule_version: str,
        source_watermark: datetime | None,
        evaluated_at: datetime,
    ) -> SnapshotRef:
        current = self.refs.get(audience_key)
        assert current is not None and current.snapshot_version == expected_version
        updated = SnapshotRef(
            audience_key,
            new_version,
            object_key,
            manifest_key,
            bitmap_sha256,
            rule_version,
            source_watermark,
            evaluated_at,
        )
        self.refs[audience_key] = updated
        return updated


class MemoryStore:
    def __init__(self, codec: BitmapCodec) -> None:
        self.codec = codec
        self.objects: dict[str, bytes] = {}

    def snapshot_key(self, audience_key: str, version: int, digest: str) -> str:
        return f"{audience_key}/v{version}/bitmap-{digest}"

    def manifest_key(self, audience_key: str, version: int, digest: str) -> str:
        return f"{audience_key}/v{version}/manifest-{digest}"

    def put_immutable(self, key: str, payload: bytes) -> None:
        existing = self.objects.setdefault(key, payload)
        assert existing == payload

    def get_bitmap(self, snapshot: SnapshotRef) -> BitMap:
        if snapshot.object_key is None:
            raise SnapshotMissing("snapshot key is missing")
        try:
            return self.codec.decode(self.objects[snapshot.object_key])
        except KeyError as exc:
            raise SnapshotMissing("snapshot bytes are missing") from exc


def period(farm_id: int, month: int, amount: int, day: int = 28) -> MeasurementPeriod:
    watermark = datetime(2026, month, day, 12, tzinfo=UTC)
    return MeasurementPeriod(
        farm_id=farm_id,
        period_start=date(2026, month, 1),
        consumed=Decimal(amount),
        source_watermark=watermark,
    )


def audience_config() -> WaterAudienceConfig:
    return WaterAudienceConfig(
        key="high_water_consumption",
        version="1",
        enabled=True,
        subject="farm",
        member_id_space="farm_owner_user_id",
        source="postgres",
        metric="normalized_water_consumption",
        period="last_complete_month",
        baseline="previous_consecutive_months",
        activation_pct=25,
        resolution_pct=15,
        min_baseline_samples=3,
        max_observation_age_days=45,
        unknown_policy="preserve_previous_membership",
        first_run_push=False,
    )


def test_publisher_bootstraps_then_publishes_only_valid_entry_and_resolution() -> None:
    config = audience_config()
    farms = {
        1: FarmData(
            1, {1, 2}, [period(1, 6, 100), period(1, 7, 100), period(1, 8, 100), period(1, 9, 130)]
        ),
        2: FarmData(
            2, {3}, [period(2, 6, 102), period(2, 7, 99), period(2, 8, 101), period(2, 9, 100)]
        ),
    }
    source = MemorySource(farms)
    metadata = MemoryMetadata()
    codec = BitmapCodec()
    store = MemoryStore(codec)
    publisher = AudiencePublisher(source, metadata, store, codec)

    first = publisher.publish(config, evaluated_at=datetime(2026, 10, 1, tzinfo=UTC))
    assert (first.snapshot_version, first.member_count, first.added_count) == (1, 2, 0)

    unchanged = publisher.publish(config, evaluated_at=datetime(2026, 10, 2, tzinfo=UTC))
    assert unchanged.status == "UNCHANGED"
    assert unchanged.snapshot_version == 1

    farms[2].periods[-1] = period(2, 9, 180, day=30)
    entered = publisher.publish(config, evaluated_at=datetime(2026, 10, 3, tzinfo=UTC))
    assert (entered.snapshot_version, entered.added_count, entered.member_count) == (2, 1, 3)

    farms[2].periods.append(period(2, 10, 100, day=30))
    resolved = publisher.publish(
        config,
        evaluated_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
    )
    assert (resolved.snapshot_version, resolved.resolved_count, resolved.member_count) == (3, 1, 2)


def test_publisher_rebuilds_snapshot_after_redis_reset() -> None:
    config = audience_config()
    farms = {
        1: FarmData(
            1, {1, 2}, [period(1, 6, 100), period(1, 7, 100), period(1, 8, 100), period(1, 9, 130)]
        )
    }
    metadata = MemoryMetadata()
    codec = BitmapCodec()
    store = MemoryStore(codec)
    publisher = AudiencePublisher(MemorySource(farms), metadata, store, codec)

    initial = publisher.publish(config, evaluated_at=datetime(2026, 10, 1, tzinfo=UTC))
    store.objects.clear()
    recovered = publisher.publish(config, evaluated_at=datetime(2026, 10, 2, tzinfo=UTC))

    ref = metadata.get_snapshot_ref(config.key)
    assert initial.snapshot_version == recovered.snapshot_version == 1
    assert recovered.status == "PUBLISHED"
    assert list(store.get_bitmap(ref)) == [1, 2]


def test_late_farm_measurement_is_not_blocked_by_another_farm_watermark() -> None:
    config = audience_config()
    farms = {
        1: FarmData(
            1,
            {1},
            [period(1, 7, 100), period(1, 8, 100), period(1, 9, 100), period(1, 10, 130, day=31)],
        )
    }
    source = MemorySource(farms)
    metadata = MemoryMetadata()
    codec = BitmapCodec()
    store = MemoryStore(codec)
    publisher = AudiencePublisher(source, metadata, store, codec)

    first = publisher.publish(config, evaluated_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert (first.snapshot_version, first.member_count) == (1, 1)

    farms[2] = FarmData(
        2,
        {2},
        [period(2, 7, 100), period(2, 8, 100), period(2, 9, 100), period(2, 10, 130, day=20)],
    )
    late_measurement = publisher.publish(config, evaluated_at=datetime(2026, 11, 2, tzinfo=UTC))

    assert (late_measurement.snapshot_version, late_measurement.added_count) == (2, 1)
    assert late_measurement.member_count == 2


def test_publisher_refuses_empty_rebuild_when_all_farms_are_unknown() -> None:
    config = audience_config()
    farms = {
        1: FarmData(
            1, {1, 2}, [period(1, 6, 100), period(1, 7, 100), period(1, 8, 100), period(1, 9, 130)]
        )
    }
    metadata = MemoryMetadata()
    codec = BitmapCodec()
    store = MemoryStore(codec)
    publisher = AudiencePublisher(MemorySource(farms), metadata, store, codec)

    publisher.publish(config, evaluated_at=datetime(2026, 10, 1, tzinfo=UTC))
    store.objects.clear()
    farms[1].periods.clear()

    with pytest.raises(NoValidEvaluation):
        publisher.publish(config, evaluated_at=datetime(2026, 10, 2, tzinfo=UTC))

    assert metadata.get_snapshot_ref(config.key).snapshot_version == 1


def test_tip_publisher_preserves_stale_membership_then_resolves_valid_data() -> None:
    config = default_tip_audiences()[0].model_copy(update={"enabled": True})
    evaluated_at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    fresh = datetime(2026, 10, 1, 10, tzinfo=UTC)
    active_signals: dict[str, bool | int | float] = {
        "nipple_setup_out_of_age_target": True,
        "nipple_flow_ml_min": 75,
    }
    safe_signals: dict[str, bool | int | float] = {
        "nipple_setup_out_of_age_target": False,
        "nipple_flow_ml_min": 75,
    }
    source = MemorySource({}, {1: {101, 102}, 2: {201}})
    metadata = MemoryMetadata()
    metadata.observations = {
        1: TipObservation(1, config.tip_id, fresh, active_signals),
        2: TipObservation(2, config.tip_id, fresh + timedelta(hours=1), safe_signals),
    }
    codec = BitmapCodec()
    store = MemoryStore(codec)
    publisher = AudiencePublisher(source, metadata, store, codec)

    first = publisher.publish(config, evaluated_at=evaluated_at)
    assert (first.member_count, first.evaluated_farms, first.unknown_farms) == (2, 2, 0)
    assert list(store.get_bitmap(metadata.get_snapshot_ref(config.key))) == [101, 102]
    assert first.source_watermark == fresh

    metadata.observations[1] = TipObservation(
        1,
        config.tip_id,
        datetime(2026, 9, 29, 10, tzinfo=UTC),
        active_signals,
    )
    stale = publisher.publish(config, evaluated_at=evaluated_at)
    assert (stale.member_count, stale.unknown_farms) == (2, 1)
    assert list(store.get_bitmap(metadata.get_snapshot_ref(config.key))) == [101, 102]
    assert stale.source_watermark is None
    assert metadata.get_snapshot_ref(config.key).source_watermark is None
    metadata.observations.pop(2)
    all_unknown = publisher.publish(config, evaluated_at=evaluated_at)
    assert (all_unknown.status, all_unknown.evaluated_farms) == ("UNCHANGED_UNKNOWN", 0)
    assert all_unknown.source_watermark is None
    assert list(store.get_bitmap(metadata.get_snapshot_ref(config.key))) == [101, 102]

    metadata.observations[1] = TipObservation(1, config.tip_id, fresh, safe_signals)
    metadata.observations[2] = TipObservation(
        2, config.tip_id, fresh + timedelta(hours=1), safe_signals
    )
    resolved = publisher.publish(config, evaluated_at=evaluated_at)
    assert (resolved.member_count, resolved.resolved_count) == (0, 2)
    assert list(store.get_bitmap(metadata.get_snapshot_ref(config.key))) == []


def test_tip_publisher_revokes_removed_owner_when_observation_is_unknown() -> None:
    config = default_tip_audiences()[0].model_copy(update={"enabled": True})
    evaluated_at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    fresh = datetime(2026, 10, 1, 10, tzinfo=UTC)
    signals: dict[str, bool | int | float] = {
        "nipple_setup_out_of_age_target": True,
        "nipple_flow_ml_min": 75,
    }
    source = MemorySource({}, {1: {101, 102}})
    metadata = MemoryMetadata()
    metadata.observations = {1: TipObservation(1, config.tip_id, fresh, signals)}
    codec = BitmapCodec()
    store = MemoryStore(codec)
    publisher = AudiencePublisher(source, metadata, store, codec)

    publisher.publish(config, evaluated_at=evaluated_at)
    source.owner_ids[1] = {102}
    metadata.observations.clear()

    result = publisher.publish(config, evaluated_at=evaluated_at)

    assert result.access_revoked_count == 1
    assert list(store.get_bitmap(metadata.get_snapshot_ref(config.key))) == [102]
    assert metadata.get_snapshot_ref(config.key).source_watermark is None

def test_tip_publisher_refuses_bootstrap_without_fresh_observations() -> None:
    config = default_tip_audiences()[0].model_copy(update={"enabled": True})
    metadata = MemoryMetadata()
    codec = BitmapCodec()
    publisher = AudiencePublisher(
        MemorySource({}, {1: {101}}),
        metadata,
        MemoryStore(codec),
        codec,
    )

    with pytest.raises(NoValidEvaluation):
        publisher.publish(config, evaluated_at=datetime(2026, 10, 1, tzinfo=UTC))

    assert metadata.refs == {}
