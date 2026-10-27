from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, sleep
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


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


@dataclass(frozen=True, slots=True)
class Tip:
    id: int
    tip: str


@dataclass(frozen=True, slots=True)
class TipAudienceLink:
    tip_id: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class TipObservation:
    farm_id: int
    tip_id: int
    observed_at: datetime
    signals: dict[str, bool | int | float]


class MetadataPostgresRepository:
    """Persists audience pointers and reviewed tip links; snapshot bytes live in Redis."""

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
        # shortcut: s3_* stores Redis keys; rename them in a production schema migration.
        return SnapshotRef(
            audience_key=str(row["audience_key"]),
            snapshot_version=int(row["snapshot_version"]),
            object_key=row["s3_object_key"],
            manifest_key=row["s3_manifest_key"],
            bitmap_sha256=row["bitmap_sha256"],
            rule_version=str(row["rule_version"]),
            source_watermark=row["source_watermark"],
            evaluated_at=row["evaluated_at"],
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
                WHERE refs.enabled IS TRUE
                  AND refs.audience_key = ANY(%s)
                ORDER BY refs.audience_key, refs.tip_id
                """,
                (audience_keys,),
            )
            result: dict[str, list[int]] = {key: [] for key in audience_keys}
            for row in cursor.fetchall():
                result[str(row["audience_key"])].append(int(row["tip_id"]))
        return result

    def list_tips(self, search: str | None, limit: int, offset: int) -> list[Tip]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, tip
                FROM public.tips
                WHERE (%s::text IS NULL OR position(%s in lower(tip)) > 0)
                ORDER BY id
                LIMIT %s OFFSET %s
                """,
                (
                    search.lower() if search else None,
                    search.lower() if search else None,
                    limit,
                    offset,
                ),
            )
            return [Tip(id=int(row["id"]), tip=str(row["tip"])) for row in cursor.fetchall()]

    def tip_links_for_audiences(self, audience_keys: list[str]) -> dict[str, list[TipAudienceLink]]:
        if not audience_keys:
            return {}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT audience_key, tip_id, enabled
                FROM tip_audience_refs
                WHERE audience_key = ANY(%s)
                ORDER BY audience_key, tip_id
                """,
                (audience_keys,),
            )
            result: dict[str, list[TipAudienceLink]] = {key: [] for key in audience_keys}
            for row in cursor.fetchall():
                result[str(row["audience_key"])].append(
                    TipAudienceLink(tip_id=int(row["tip_id"]), enabled=bool(row["enabled"]))
                )
        return result

    def upsert_tip_observation(
        self,
        farm_id: int,
        tip_id: int,
        observed_at: datetime,
        signals: dict[str, bool | int | float],
    ) -> bool:
        if observed_at.tzinfo is None:
            raise ValueError("observation timestamp must be timezone-aware")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.farm_tip_observations
                    (farm_id, tip_id, observed_at, signals)
                SELECT farms.id, tips.id, %s, %s
                FROM public.farms AS farms
                JOIN public.tips AS tips ON tips.id = %s
                WHERE farms.id = %s
                ON CONFLICT (farm_id, tip_id) DO UPDATE
                SET observed_at = EXCLUDED.observed_at,
                    signals = EXCLUDED.signals,
                    updated_at = statement_timestamp()
                RETURNING farm_id
                """,
                (observed_at, Jsonb(signals), tip_id, farm_id),
            )
            return cursor.fetchone() is not None

    def tip_observations(self, tip_id: int) -> dict[int, TipObservation]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT farm_id, tip_id, observed_at, signals
                FROM public.farm_tip_observations
                WHERE tip_id = %s
                ORDER BY farm_id
                """,
                (tip_id,),
            )
            return {
                int(row["farm_id"]): TipObservation(
                    farm_id=int(row["farm_id"]),
                    tip_id=int(row["tip_id"]),
                    observed_at=row["observed_at"],
                    signals=dict(row["signals"]),
                )
                for row in cursor.fetchall()
            }

    def link_tip(
        self,
        audience_key: str,
        tip_id: int,
        rule_version: str,
        *,
        enabled: bool = True,
    ) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM public.tips WHERE id = %s", (tip_id,))
            if cursor.fetchone() is None:
                return False
            cursor.execute(
                """
                INSERT INTO audience_snapshot_refs (audience_key, rule_version)
                VALUES (%s, %s)
                ON CONFLICT (audience_key) DO NOTHING
                """,
                (audience_key, rule_version),
            )
            cursor.execute(
                """
                INSERT INTO tip_audience_refs (tip_id, audience_key, enabled)
                VALUES (%s, %s, %s)
                ON CONFLICT (tip_id, audience_key) DO UPDATE
                SET enabled = EXCLUDED.enabled
                """,
                (tip_id, audience_key, enabled),
            )
        return True

    def unlink_tip(self, audience_key: str, tip_id: int) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM tip_audience_refs WHERE audience_key = %s AND tip_id = %s",
                (audience_key, tip_id),
            )

    def delete_audience(self, audience_key: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM tip_audience_refs WHERE audience_key = %s", (audience_key,))
            cursor.execute(
                "DELETE FROM audience_snapshot_refs WHERE audience_key = %s", (audience_key,)
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
