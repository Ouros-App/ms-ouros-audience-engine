import hashlib
from types import SimpleNamespace

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_cache import BitmapReader
from ouros_audience_engine.repositories.metadata_postgres import SnapshotRef


class FakeRedis:
    def __init__(self, value: bytes | None = None, fail: bool = False) -> None:
        self.value = value
        self.fail = fail
        self.set_calls = 0
        self.deleted = 0

    def get(self, _key: str) -> bytes | None:
        if self.fail:
            raise ConnectionError("redis down")
        return self.value

    def set(self, _key: str, value: bytes, ex: int) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        assert ex == 60
        self.set_calls += 1
        self.value = value

    def delete(self, _key: str) -> None:
        self.deleted += 1
        self.value = None


class FakeS3:
    def __init__(self, payload: bytes, count: int) -> None:
        self.payload = payload
        self.count = count
        self.calls = 0

    def get_snapshot(self, **_kwargs: object) -> tuple[SimpleNamespace, bytes]:
        self.calls += 1
        return SimpleNamespace(member_count=self.count), self.payload


def snapshot(payload: bytes) -> SnapshotRef:
    return SnapshotRef(
        audience_key="high_water_consumption",
        snapshot_version=4,
        object_key="audiences/high_water_consumption/v0000000004/bitmap.roaring",
        manifest_key="audiences/high_water_consumption/v0000000004/manifest.json",
        bitmap_sha256=hashlib.sha256(payload).hexdigest(),
        rule_version="1",
        source_watermark=None,
        evaluated_at=None,
        dispatched_through_version=0,
    )


def test_cache_miss_loads_s3_and_populates_binary_cache() -> None:
    codec = BitmapCodec()
    payload = codec.encode([2, 7])
    redis = FakeRedis()
    s3 = FakeS3(payload, count=2)
    bitmap = BitmapReader(redis, s3, codec, ttl_seconds=60).get_bitmap(snapshot(payload))
    assert list(bitmap) == [2, 7]
    assert redis.value == payload
    assert redis.set_calls == 1
    assert s3.calls == 1


def test_valid_cache_hit_skips_s3() -> None:
    codec = BitmapCodec()
    payload = codec.encode([9])
    redis = FakeRedis(payload)
    s3 = FakeS3(payload, count=1)
    bitmap = BitmapReader(redis, s3, codec, ttl_seconds=60).get_bitmap(snapshot(payload))
    assert list(bitmap) == [9]
    assert s3.calls == 0


def test_corrupted_cache_is_invalidated_and_recovered_from_s3() -> None:
    codec = BitmapCodec()
    payload = codec.encode([9])
    redis = FakeRedis(b"corrupt")
    s3 = FakeS3(payload, count=1)
    bitmap = BitmapReader(redis, s3, codec, ttl_seconds=60).get_bitmap(snapshot(payload))
    assert list(bitmap) == [9]
    assert redis.deleted == 1
    assert s3.calls == 1


def test_redis_outage_uses_verified_s3_without_returning_empty_membership() -> None:
    codec = BitmapCodec()
    payload = codec.encode([9])
    s3 = FakeS3(payload, count=1)
    bitmap = BitmapReader(FakeRedis(fail=True), s3, codec, ttl_seconds=60).get_bitmap(
        snapshot(payload)
    )
    assert list(bitmap) == [9]


def test_s3_failure_is_not_masqueraded_as_an_empty_bitmap() -> None:
    class BrokenS3:
        def get_snapshot(self, **_kwargs: object) -> None:
            raise OSError("s3 down")

    codec = BitmapCodec()
    payload = codec.encode([1])
    reader = BitmapReader(FakeRedis(), BrokenS3(), codec, ttl_seconds=60)
    try:
        reader.get_bitmap(snapshot(payload))
    except Exception as exc:
        assert exc.__class__.__name__ == "SnapshotUnavailable"
    else:
        raise AssertionError("S3 failure must fail closed")
