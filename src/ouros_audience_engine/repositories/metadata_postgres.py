from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, sleep
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row


class ConcurrentPublishError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    audience_key: str
    snapshot_version: int
    object_key: str | None
    manifest_key: str | None
    bitmap_sha256: str | None
    rule_version: str
    source_watermark: datetime | None
    evaluated_at: datetime | None
    dispatched_through_version: int


class MetadataPostgresRepository:
    """Persists only audience pointers, reviewed tip links and one cursor per audience."""

    def __init__(self, database_url: str, timeout_seconds: int = 5) -> None:
        self.database_url = database_url
        self.timeout_seconds = timeout_seconds

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return cast(
            psycopg.Connection[dict[str, Any]],
            psycopg.connect(
                self.database_url,
                connect_timeout=self.timeout_seconds,
                row_factory=dict_row,
            ),
        )

    @staticmethod
    def _snapshot(row: dict[str, Any]) -> SnapshotRef:
        return SnapshotRef(
            audience_key=str(row["audience_key"]),
            snapshot_version=int(row["snapshot_version"]),
            object_key=row["s3_object_key"],
            manifest_key=row["s3_manifest_key"],
            bitmap_sha256=row["bitmap_sha256"],
            rule_version=str(row["rule_version"]),
            source_watermark=row["source_watermark"],
            evaluated_at=row["evaluated_at"],
            dispatched_through_version=int(row["dispatched_through_version"]),
        )

    @contextmanager
    def publish_job_lock(self, timeout_seconds: float = 60) -> Iterator[None]:
        if timeout_seconds <= 0:
            raise ValueError("job lock timeout must be positive")
        lock_name = "ouros-audience-engine:publish"
        deadline = monotonic() + timeout_seconds
        with self._connect() as connection:
            connection.autocommit = True
            acquired = False
            while not acquired:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                        (lock_name,),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("PostgreSQL returned no advisory lock result")
                acquired = bool(row["acquired"])
                if not acquired:
                    if monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for audience publish lock")
                    sleep(0.1)
            try:
                yield
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (lock_name,),
                    )

    def check(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def ensure_audience(self, audience_key: str, rule_version: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audience_snapshot_refs (audience_key, rule_version)
                VALUES (%s, %s)
                ON CONFLICT (audience_key) DO NOTHING
                """,
                (audience_key, rule_version),
            )

    def get_snapshot_ref(self, audience_key: str) -> SnapshotRef:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM audience_snapshot_refs WHERE audience_key = %s",
                (audience_key,),
            )
            row = cursor.fetchone()
        if row is None:
            raise LookupError(f"audience metadata is not initialized: {audience_key}")
        return self._snapshot(row)

    def enabled_tip_ids(self, audience_keys: list[str]) -> dict[str, list[int]]:
        if not audience_keys:
            return {}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT refs.audience_key, refs.tip_id
                FROM tip_audience_refs AS refs
                JOIN audience_snapshot_refs AS snapshots USING (audience_key)
                WHERE refs.enabled IS TRUE
                  AND snapshots.snapshot_version > 0
                  AND refs.audience_key = ANY(%s)
                ORDER BY refs.audience_key, refs.tip_id
                """,
                (audience_keys,),
            )
            result: dict[str, list[int]] = {key: [] for key in audience_keys}
            for row in cursor.fetchall():
                result[str(row["audience_key"])].append(int(row["tip_id"]))
        return result

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
        if new_version not in (expected_version, expected_version + 1):
            raise ValueError("snapshot version must stay unchanged or advance by one")
        with self._connect() as connection, connection.cursor() as cursor:
            if expected_version == 0:
                cursor.execute(
                    """
                    UPDATE audience_snapshot_refs
                    SET snapshot_version = %s, s3_object_key = %s, s3_manifest_key = %s,
                        bitmap_sha256 = %s, rule_version = %s, source_watermark = %s,
                        evaluated_at = %s,
                        published_at = CASE WHEN %s > 0 THEN %s ELSE published_at END
                    WHERE audience_key = %s AND snapshot_version = 0
                    RETURNING *
                    """,
                    (
                        new_version,
                        object_key,
                        manifest_key,
                        bitmap_sha256,
                        rule_version,
                        source_watermark,
                        evaluated_at,
                        new_version,
                        evaluated_at,
                        audience_key,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE audience_snapshot_refs
                    SET snapshot_version = %s, s3_object_key = %s, s3_manifest_key = %s,
                        bitmap_sha256 = %s, rule_version = %s, source_watermark = %s,
                        evaluated_at = %s,
                        published_at = CASE WHEN %s > snapshot_version THEN %s ELSE published_at END
                    WHERE audience_key = %s AND snapshot_version = %s
                    RETURNING *
                    """,
                    (
                        new_version,
                        object_key,
                        manifest_key,
                        bitmap_sha256,
                        rule_version,
                        source_watermark,
                        evaluated_at,
                        new_version,
                        evaluated_at,
                        audience_key,
                        expected_version,
                    ),
                )
            row = cursor.fetchone()
            if row is None:
                raise ConcurrentPublishError(f"snapshot pointer changed: {audience_key}")
        return self._snapshot(row)

    def advance_dispatch_cursor(
        self, audience_key: str, expected_version: int, new_version: int
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE audience_snapshot_refs
                SET dispatched_through_version = %s
                WHERE audience_key = %s
                  AND dispatched_through_version = %s
                  AND snapshot_version >= %s
                """,
                (new_version, audience_key, expected_version, new_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentPublishError(f"dispatch cursor changed: {audience_key}")

    def active_audiences(self) -> list[SnapshotRef]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT snapshots.*
                FROM audience_snapshot_refs AS snapshots
                JOIN tip_audience_refs AS refs USING (audience_key)
                WHERE snapshots.snapshot_version > 0 AND refs.enabled IS TRUE
                ORDER BY snapshots.audience_key
                """
            )
            return [self._snapshot(row) for row in cursor.fetchall()]
