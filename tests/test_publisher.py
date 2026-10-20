from datetime import UTC, date, datetime
from decimal import Decimal

from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.domain.audience import MeasurementPeriod
from ouros_audience_engine.domain.config import WaterAudienceConfig
from ouros_audience_engine.repositories.metadata_postgres import SnapshotRef
from ouros_audience_engine.repositories.source_postgres import FarmData
from ouros_audience_engine.services.audience_publisher import AudiencePublisher


class MemorySource:
    def __init__(self, farms: dict[int, FarmData]) -> None:
        self.farms = farms

    def load_water_audience_inputs(self, evaluated_at: datetime) -> dict[int, FarmData]:
        return self.farms


class MemoryMetadata:
    def __init__(self) -> None:
        self.refs: dict[str, SnapshotRef] = {}

    def get_snapshot_ref(self, audience_key: str) -> SnapshotRef:
        try:
            return self.refs[audience_key]
        except KeyError as exc:
            raise LookupError(audience_key) from exc

    def ensure_audience(self, audience_key: str, rule_version: str) -> None:
        self.refs.setdefault(
            audience_key,
            SnapshotRef(audience_key, 0, None, None, None, rule_version, None, None, 0),
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
            current.dispatched_through_version,
        )
        self.refs[audience_key] = updated
        return updated


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def snapshot_key(self, audience_key: str, version: int, digest: str) -> str:
        return f"{audience_key}/v{version}/bitmap-{digest}"

    def transition_key(self, audience_key: str, version: int, kind: str, digest: str) -> str:
        return f"{audience_key}/v{version}/{kind}-{digest}"

    def manifest_key(self, audience_key: str, version: int, digest: str) -> str:
        return f"{audience_key}/v{version}/manifest-{digest}"

    def put_immutable(self, key: str, payload: bytes, content_type: str) -> None:
        assert content_type in {"application/vnd.roaringbitmap", "application/json"}
        existing = self.objects.setdefault(key, payload)
        assert existing == payload


class MemoryReader:
    def __init__(self, store: MemoryStore, codec: BitmapCodec) -> None:
        self.store = store
        self.codec = codec

    def get_bitmap(self, snapshot: SnapshotRef) -> BitMap:
        assert snapshot.object_key is not None
        return self.codec.decode(self.store.objects[snapshot.object_key])


def period(farm_id: int, month: int, amount: int, day: int = 28) -> MeasurementPeriod:
    watermark = datetime(2026, month, day, 12, tzinfo=UTC)
    return MeasurementPeriod(
        farm_id=farm_id,
        period_start=date(2026, month, 1),
        consumed=Decimal(amount),
        source_watermark=watermark,
    )


def test_publisher_bootstraps_then_publishes_only_valid_entry_and_resolution() -> None:
    config = WaterAudienceConfig(
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
    store = MemoryStore()
    codec = BitmapCodec()
    publisher = AudiencePublisher(source, metadata, store, codec, MemoryReader(store, codec))

    first = publisher.publish(config, evaluated_at=datetime(2026, 10, 1, tzinfo=UTC))
    assert (first.snapshot_version, first.member_count, first.added_count) == (1, 2, 0)

    unchanged = publisher.publish(config, evaluated_at=datetime(2026, 10, 2, tzinfo=UTC))
    assert unchanged.status == "UNCHANGED_UNKNOWN"
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
