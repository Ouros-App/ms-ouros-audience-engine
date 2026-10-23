from __future__ import annotations

import hashlib
import re
from typing import cast

import redis
from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.domain.manifest import SnapshotManifest
from ouros_audience_engine.repositories.metadata_postgres import SnapshotRef


class SnapshotUnavailable(RuntimeError):
    pass


class SnapshotMissing(SnapshotUnavailable):
    pass


class ImmutableSnapshotConflict(RuntimeError):
    pass


class RedisSnapshotStore:
    """Stores immutable, versioned snapshots in Redis without expiry."""

    def __init__(self, redis_client: redis.Redis, codec: BitmapCodec) -> None:
        self.redis_client = redis_client
        self.codec = codec

    def snapshot_key(self, audience_key: str, version: int, digest: str) -> str:
        self._validate_key_parts(audience_key, version, digest)
        return f"ouros:audience:bitmap:{audience_key}:v{version}:{digest}"

    def manifest_key(self, audience_key: str, version: int, digest: str) -> str:
        self._validate_key_parts(audience_key, version, digest)
        return f"ouros:audience:manifest:{audience_key}:v{version}:{digest}"

    def put_immutable(self, key: str, payload: bytes) -> None:
        if self.redis_client.set(key, payload, nx=True):
            return
        existing = cast(bytes | None, self.redis_client.get(key))
        if existing != payload:
            raise ImmutableSnapshotConflict(f"immutable Redis snapshot key conflicts: {key}")

    def get_bitmap(self, snapshot: SnapshotRef) -> BitMap:
        if (
            not snapshot.object_key
            or not snapshot.manifest_key
            or not snapshot.bitmap_sha256
            or snapshot.snapshot_version < 1
        ):
            raise SnapshotUnavailable("published audience snapshot is incomplete")
        try:
            manifest_payload = cast(bytes | None, self.redis_client.get(snapshot.manifest_key))
            if manifest_payload is None:
                raise SnapshotMissing("audience snapshot manifest is missing from Redis")
            manifest = SnapshotManifest.model_validate_json(manifest_payload)
            manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
            expected_manifest_key = self.manifest_key(
                snapshot.audience_key, snapshot.snapshot_version, manifest_digest
            )
            expected_bitmap_key = self.snapshot_key(
                snapshot.audience_key, snapshot.snapshot_version, manifest.sha256
            )
            if (
                snapshot.manifest_key != expected_manifest_key
                or snapshot.object_key != expected_bitmap_key
                or manifest.audience_key != snapshot.audience_key
                or manifest.snapshot_version != snapshot.snapshot_version
                or manifest.object_key != expected_bitmap_key
                or manifest.sha256 != snapshot.bitmap_sha256
                or manifest.rule_version != snapshot.rule_version
                or manifest.evaluated_at != snapshot.evaluated_at
                or manifest.source_watermark != snapshot.source_watermark
            ):
                raise ValueError("snapshot manifest does not match the PostgreSQL pointer")
            payload = cast(bytes | None, self.redis_client.get(snapshot.object_key))
            if payload is None:
                raise SnapshotMissing("audience bitmap is missing from Redis")
            if hashlib.sha256(payload).hexdigest() != snapshot.bitmap_sha256:
                raise ValueError("audience bitmap SHA-256 does not match the PostgreSQL pointer")
            bitmap = self.codec.decode(payload)
            if len(bitmap) != manifest.member_count:
                raise ValueError("audience member count does not match its manifest")
            return bitmap
        except SnapshotMissing:
            raise
        except Exception as exc:
            raise SnapshotUnavailable("could not load a valid Redis audience snapshot") from exc

    def delete_audience(self, audience_key: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", audience_key):
            raise ValueError("invalid audience key")
        for kind in ("bitmap", "manifest"):
            pattern = f"ouros:audience:{kind}:{audience_key}:*"
            for key in self.redis_client.scan_iter(match=pattern):
                self.redis_client.unlink(key)

    @staticmethod
    def _validate_key_parts(audience_key: str, version: int, digest: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", audience_key):
            raise ValueError("invalid audience key")
        if version < 1:
            raise ValueError("snapshot version must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("snapshot digest must be a SHA-256 hex string")
