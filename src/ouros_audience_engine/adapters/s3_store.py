from __future__ import annotations

import hashlib
from io import BytesIO
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from ouros_audience_engine.domain.manifest import SnapshotManifest

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object


class ImmutableObjectConflict(RuntimeError):
    pass


class S3Store:
    def __init__(self, client: S3Client, bucket: str, prefix: str = "") -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def snapshot_key(self, audience_key: str, version: int, digest: str) -> str:
        self._validate_audience_key(audience_key)
        return self._key(f"audiences/{audience_key}/v{version:010d}/bitmap-{digest}.roaring")

    def manifest_key(self, audience_key: str, version: int, digest: str) -> str:
        self._validate_audience_key(audience_key)
        return self._key(f"audiences/{audience_key}/v{version:010d}/manifest-{digest}.json")

    def transition_key(self, audience_key: str, version: int, kind: str, digest: str) -> str:
        self._validate_audience_key(audience_key)
        if kind not in {"added", "removed"}:
            raise ValueError("transition kind must be added or removed")
        return self._key(
            f"audiences/{audience_key}/v{version:010d}/transitions.{kind}-{digest}.roaring"
        )

    def put_immutable(self, key: str, payload: bytes, content_type: str) -> None:
        self._validate_key(key)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404 and exc.response.get("Error", {}).get("Code") not in {
                "404",
                "NoSuchKey",
            }:
                raise
        else:
            current = self.get_bytes(key)
            if hashlib.sha256(current).hexdigest() != digest or current != payload:
                raise ImmutableObjectConflict(
                    f"immutable S3 key already has different content: {key}"
                )
            return

        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=BytesIO(payload),
            ContentLength=len(payload),
            ContentType=content_type,
        )
        stored = self.get_bytes(key)
        if hashlib.sha256(stored).hexdigest() != digest:
            raise OSError(f"S3 read-after-write checksum mismatch: {key}")

    def get_snapshot(
        self,
        *,
        audience_key: str,
        version: int,
        object_key: str,
        manifest_key: str,
        expected_sha256: str,
    ) -> tuple[SnapshotManifest, bytes]:
        self._validate_audience_key(audience_key)
        self._validate_key(object_key)
        self._validate_key(manifest_key)
        manifest_payload = self.get_bytes(manifest_key)
        manifest = SnapshotManifest.model_validate_json(manifest_payload)
        if (
            manifest.audience_key != audience_key
            or manifest.snapshot_version != version
            or manifest.object_key != object_key
            or manifest.sha256 != expected_sha256
        ):
            raise ValueError("snapshot manifest does not match the published pointer")
        payload = self.get_bytes(object_key)
        self.verify_sha256(payload, expected_sha256)
        return manifest, payload

    def get_bytes(self, key: str) -> bytes:
        self._validate_key(key)
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    @staticmethod
    def verify_sha256(payload: bytes, expected: str) -> None:
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError("snapshot SHA-256 mismatch")

    def _key(self, key: str) -> str:
        if key.startswith("/") or any(part in {".", ".."} for part in key.split("/")):
            raise ValueError("invalid S3 object key")
        result = f"{self.prefix}/{key}" if self.prefix else key
        self._validate_key(result)
        return result

    def _validate_key(self, key: str) -> None:
        if not key or key.startswith("/") or any(part in {".", ".."} for part in key.split("/")):
            raise ValueError("invalid S3 object key")
        if self.prefix and not key.startswith(f"{self.prefix}/"):
            raise ValueError("S3 object key is outside the configured prefix")

    @staticmethod
    def _validate_audience_key(value: str) -> None:
        if not value or len(value) > 63 or value[0] < "a" or value[0] > "z":
            raise ValueError("invalid audience key")
        if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in value):
            raise ValueError("invalid audience key")
