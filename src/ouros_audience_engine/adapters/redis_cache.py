import hashlib
import logging
import threading
from collections import defaultdict
from typing import cast

import redis
from pyroaring import BitMap

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.s3_store import S3Store
from ouros_audience_engine.repositories.metadata_postgres import SnapshotRef

logger = logging.getLogger(__name__)


class SnapshotUnavailable(RuntimeError):
    pass


class BitmapReader:
    """Read versioned bitmap bytes from Redis, falling back to checksum-verified S3."""

    def __init__(
        self,
        redis_client: redis.Redis,
        s3_store: S3Store,
        codec: BitmapCodec,
        *,
        ttl_seconds: int,
    ) -> None:
        self.redis_client = redis_client
        self.s3_store = s3_store
        self.codec = codec
        self.ttl_seconds = ttl_seconds
        self._key_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def get_bitmap(self, snapshot: SnapshotRef) -> BitMap:
        if (
            not snapshot.object_key
            or not snapshot.manifest_key
            or not snapshot.bitmap_sha256
            or snapshot.snapshot_version < 1
        ):
            raise SnapshotUnavailable("published audience snapshot is incomplete")
        cache_key = (
            f"ouros:audience:bitmap:{snapshot.audience_key}:v{snapshot.snapshot_version}:"
            f"{snapshot.bitmap_sha256[:16]}"
        )
        try:
            bitmap = self._read_cache(cache_key, snapshot.bitmap_sha256)
            if bitmap is not None:
                return bitmap
        except Exception:
            logger.warning("audience cache read failed; falling back to S3")

        with self._key_locks[cache_key]:
            try:
                bitmap = self._read_cache(cache_key, snapshot.bitmap_sha256)
                if bitmap is not None:
                    return bitmap
            except Exception:
                logger.warning("audience cache retry failed; falling back to S3")

            try:
                manifest, payload = self.s3_store.get_snapshot(
                    audience_key=snapshot.audience_key,
                    version=snapshot.snapshot_version,
                    object_key=snapshot.object_key,
                    manifest_key=snapshot.manifest_key,
                    expected_sha256=snapshot.bitmap_sha256,
                )
                bitmap = self.codec.decode(payload)
                if len(bitmap) != manifest.member_count:
                    raise ValueError("snapshot member count does not match its manifest")
            except Exception as exc:
                raise SnapshotUnavailable(
                    "could not load a valid published audience snapshot"
                ) from exc

            try:
                self.redis_client.set(cache_key, payload, ex=self.ttl_seconds)
            except Exception:
                logger.warning("audience cache write failed; serving verified S3 snapshot")
            return bitmap

    def _read_cache(self, cache_key: str, expected_sha256: str) -> BitMap | None:
        payload = cast(bytes | None, self.redis_client.get(cache_key))
        if payload is None:
            return None
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            self.redis_client.delete(cache_key)
            return None
        try:
            return self.codec.decode(payload)
        except ValueError:
            self.redis_client.delete(cache_key)
            return None
