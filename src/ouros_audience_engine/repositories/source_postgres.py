from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from ouros_audience_engine.domain.audience import MeasurementPeriod


@dataclass(slots=True)
class FarmData:
    farm_id: int
    owner_ids: set[int] = field(default_factory=set)
    periods: list[MeasurementPeriod] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WaterMeterAuditInputs:
    latest_reading_by_farm: dict[int, datetime | None]
    source_watermark: datetime | None


class SourcePostgresRepository:
    """Read-only adapter for the versioned OUROS farms, owners and water records."""

    def __init__(self, database_url: str, timeout_seconds: int = 5) -> None:
        self.database_url = database_url
        self.timeout_seconds = timeout_seconds

    def load_farm_owners(self) -> dict[int, set[int]]:
        farms: dict[int, set[int]] = {}
        with (
            psycopg.connect(
                self.database_url,
                connect_timeout=self.timeout_seconds,
                options="-c default_transaction_read_only=on",
                row_factory=dict_row,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT f.id AS farm_id, fo.id AS owner_id
                FROM public.farms AS f
                JOIN public.farm_owners AS fo ON fo.id_farm = f.id
                ORDER BY f.id, fo.id
                """
            )
            for row in cursor.fetchall():
                farms.setdefault(int(row["farm_id"]), set()).add(int(row["owner_id"]))
        return farms

    def load_daily_water_meter_audit_inputs(self) -> WaterMeterAuditInputs:
        latest_reading_by_farm: dict[int, datetime | None] = {}
        source_watermark: datetime | None = None
        with (
            psycopg.connect(
                self.database_url,
                connect_timeout=self.timeout_seconds,
                options="-c default_transaction_read_only=on",
                row_factory=dict_row,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                WITH source_state AS (
                    SELECT
                        farms.id AS farm_id,
                        MAX(registries.registration_date) AT TIME ZONE 'UTC' AS last_reading_at,
                        statement_timestamp() AS source_watermark
                    FROM public.farms AS farms
                    LEFT JOIN public.water_registries AS registries
                        ON registries.id_farm = farms.id
                    GROUP BY farms.id
                )
                SELECT farm_id, last_reading_at, source_watermark
                FROM source_state
                ORDER BY farm_id
                """
            )
            for row in cursor.fetchall():
                latest_reading_by_farm[int(row["farm_id"])] = row["last_reading_at"]
                source_watermark = row["source_watermark"]
        return WaterMeterAuditInputs(latest_reading_by_farm, source_watermark)

    def load_water_audience_inputs(self, evaluated_at: datetime) -> dict[int, FarmData]:
        if evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        farms: dict[int, FarmData] = {}
        connection = psycopg.connect(
            self.database_url,
            connect_timeout=self.timeout_seconds,
            options="-c default_transaction_read_only=on",
            row_factory=dict_row,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id AS farm_id, id_farm_owner AS owner_id
                    FROM (
                        SELECT f.id, fo.id AS id_farm_owner
                        FROM public.farms AS f
                        JOIN public.farm_owners AS fo ON fo.id_farm = f.id
                    ) AS farm_owners
                    ORDER BY farm_id, owner_id
                    """
                )
                for row in cursor.fetchall():
                    farm_id = int(row["farm_id"])
                    farms.setdefault(farm_id, FarmData(farm_id)).owner_ids.add(int(row["owner_id"]))

                cursor.execute(
                    """
                    SELECT
                        w.id_farm AS farm_id,
                        date_trunc('month', w.registration_date)::date AS period_start,
                        SUM(w.end_hydrometer - w.start_hydrometer)::numeric AS consumed,
                        MAX(w.registration_date) AT TIME ZONE 'UTC' AS source_watermark
                    FROM public.water_registries AS w
                    WHERE w.registration_date < date_trunc('month', %s::timestamp)
                      AND w.registration_date >= date_trunc(
                          'month', %s::timestamp
                      ) - INTERVAL '24 months'
                    GROUP BY w.id_farm, date_trunc('month', w.registration_date)::date
                    ORDER BY w.id_farm, period_start
                    """,
                    (
                        evaluated_at.astimezone(UTC).replace(tzinfo=None),
                        evaluated_at.astimezone(UTC).replace(tzinfo=None),
                    ),
                )
                for row in cursor.fetchall():
                    farm_id = int(row["farm_id"])
                    farm = farms.setdefault(farm_id, FarmData(farm_id))
                    watermark = row["source_watermark"]
                    if watermark.tzinfo is None:
                        watermark = watermark.replace(tzinfo=UTC)
                    farm.periods.append(
                        MeasurementPeriod(
                            farm_id=farm_id,
                            period_start=row["period_start"],
                            consumed=Decimal(row["consumed"]),
                            source_watermark=watermark,
                        )
                    )
        finally:
            connection.close()
        return farms
