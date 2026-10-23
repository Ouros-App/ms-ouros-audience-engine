from datetime import UTC, datetime
from fnmatch import fnmatchcase
from hashlib import sha256

import pytest

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_snapshot_store import (
    ImmutableSnapshotConflict,
    RedisSnapshotStore,
    SnapshotMissing,
    SnapshotUnavailable,
)
from ouros_audience_engine.domain.manifest import SnapshotManifest
from ouros_audience_engine.repositories.metadata_postgres import SnapshotRef


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: bytes, *, nx: bool = False) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def scan_iter(self, *, match: str):
        return (key for key in tuple(self.values) if fnmatchcase(key, match))

    def unlink(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


def published_snapshot(
    store: RedisSnapshotStore,
    redis_client: MemoryRedis,
    *,
    members: list[int] | None = None,
) -> SnapshotRef:
    evaluated_at = datetime(2026, 11, 1, tzinfo=UTC)
    source_watermark = datetime(2026, 10, 31, tzinfo=UTC)
    bitmap = BitmapCodec().encode(members or [7, 42])
    bitmap_digest = sha256(bitmap).hexdigest()
    object_key = store.snapshot_key("high_water", 1, bitmap_digest)
    manifest = SnapshotManifest(
        audience_key="high_water",
        snapshot_version=1,
        bitmap_format=BitmapCodec.format_name,
        bitmap_format_version=BitmapCodec.format_version,
        member_id_space=BitmapCodec.member_id_space,
        rule_version="1",
        object_key=object_key,
        member_count=len(members or [7, 42]),
        sha256=bitmap_digest,
        evaluated_at=evaluated_at,
        source_watermark=source_watermark,
    )
    manifest_payload = manifest.model_dump_json().encode()
    manifest_digest = sha256(manifest_payload).hexdigest()
    manifest_key = store.manifest_key("high_water", 1, manifest_digest)
    redis_client.values[object_key] = bitmap
    redis_client.values[manifest_key] = manifest_payload
    return SnapshotRef(
        "high_water",
        1,
        object_key,
        manifest_key,
        bitmap_digest,
        "1",
        source_watermark,
        evaluated_at,
    )


def test_snapshot_keys_validate_parts_and_encode_versioned_content() -> None:
    store = RedisSnapshotStore(MemoryRedis(), BitmapCodec())
    digest = "a" * 64
    assert store.snapshot_key("high_water", 2, digest) == (
        f"ouros:audience:bitmap:high_water:v2:{digest}"
    )
    assert store.manifest_key("high_water", 2, digest) == (
        f"ouros:audience:manifest:high_water:v2:{digest}"
    )
    with pytest.raises(ValueError):
        store.snapshot_key("Bad-Key", 1, digest)
    with pytest.raises(ValueError):
        store.snapshot_key("high_water", 0, digest)
    with pytest.raises(ValueError):
        store.manifest_key("high_water", 1, "not-a-digest")


def test_immutable_snapshot_is_idempotent_but_rejects_conflicting_bytes() -> None:
    redis_client = MemoryRedis()
    store = RedisSnapshotStore(redis_client, BitmapCodec())

    store.put_immutable("snapshot:v1", b"first")
    store.put_immutable("snapshot:v1", b"first")
    assert redis_client.values["snapshot:v1"] == b"first"
    with pytest.raises(ImmutableSnapshotConflict):
        store.put_immutable("snapshot:v1", b"different")


def test_get_bitmap_verifies_manifest_pointer_checksum_and_member_count() -> None:
    redis_client = MemoryRedis()
    store = RedisSnapshotStore(redis_client, BitmapCodec())
    snapshot = published_snapshot(store, redis_client)

    assert list(store.get_bitmap(snapshot)) == [7, 42]

    redis_client.values[snapshot.object_key] = b"corrupt"
    with pytest.raises(SnapshotUnavailable):
        store.get_bitmap(snapshot)


def test_get_bitmap_reports_missing_manifest_or_bitmap() -> None:
    redis_client = MemoryRedis()
    store = RedisSnapshotStore(redis_client, BitmapCodec())
    snapshot = published_snapshot(store, redis_client)
    del redis_client.values[snapshot.manifest_key]
    with pytest.raises(SnapshotMissing, match="manifest"):
        store.get_bitmap(snapshot)

    snapshot = published_snapshot(store, redis_client)
    del redis_client.values[snapshot.object_key]
    with pytest.raises(SnapshotMissing, match="bitmap"):
        store.get_bitmap(snapshot)


def test_get_bitmap_rejects_incomplete_pointer_and_mismatched_manifest() -> None:
    redis_client = MemoryRedis()
    store = RedisSnapshotStore(redis_client, BitmapCodec())
    snapshot = published_snapshot(store, redis_client)

    with pytest.raises(SnapshotUnavailable, match="incomplete"):
        store.get_bitmap(SnapshotRef("high_water", 0, None, None, None, "1", None, None))

    altered = SnapshotRef(
        snapshot.audience_key,
        snapshot.snapshot_version,
        snapshot.object_key,
        snapshot.manifest_key,
        snapshot.bitmap_sha256,
        "2",
        snapshot.source_watermark,
        snapshot.evaluated_at,
    )
    with pytest.raises(SnapshotUnavailable, match="valid Redis"):
        store.get_bitmap(altered)


def test_delete_audience_removes_its_versions_without_touching_others() -> None:
    redis_client = MemoryRedis()
    store = RedisSnapshotStore(redis_client, BitmapCodec())
    redis_client.values.update(
        {
            "ouros:audience:bitmap:high_water:v1:a": b"bitmap",
            "ouros:audience:manifest:high_water:v1:b": b"manifest",
            "ouros:audience:bitmap:other_audience:v1:c": b"other",
        }
    )

    store.delete_audience("high_water")

    assert redis_client.values == {
        "ouros:audience:bitmap:other_audience:v1:c": b"other"
    }
    with pytest.raises(ValueError):
        store.delete_audience("bad-key")
