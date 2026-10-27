from datetime import UTC, datetime
from os import environ
from uuid import uuid4

import pytest

from ouros_audience_engine.repositories.metadata_postgres import (
    ConcurrentPublishError,
    MetadataPostgresRepository,
)
from ouros_audience_engine.repositories.source_postgres import SourcePostgresRepository


@pytest.fixture
def database_url() -> str:
    value = environ.get("AUDIENCE_TEST_DATABASE_URL")
    if not value:
        pytest.skip("set AUDIENCE_TEST_DATABASE_URL to run PostgreSQL integration tests")
    return value


def test_source_repository_reads_seeded_farms_and_monthly_water(database_url: str) -> None:
    farms = SourcePostgresRepository(database_url).load_water_audience_inputs(
        datetime(2026, 11, 1, tzinfo=UTC)
    )

    assert farms[1].owner_ids == {1, 2}
    assert farms[2].owner_ids == {3}
    assert farms[1].periods[-1].consumed == 130
    assert farms[1].periods[-1].period_start.isoformat() == "2026-09-01"



def test_source_repository_reads_latest_meter_timestamp_for_farms(database_url: str) -> None:
    inputs = SourcePostgresRepository(database_url).load_daily_water_meter_audit_inputs()

    assert inputs.latest_reading_by_farm[1] == datetime(2026, 9, 28, tzinfo=UTC)
    assert inputs.latest_reading_by_farm[2] == datetime(2026, 9, 28, tzinfo=UTC)
    assert inputs.source_watermark is not None
    assert inputs.source_watermark.tzinfo is not None

def test_metadata_repository_publishes_links_and_enforces_snapshot_cas(
    database_url: str,
) -> None:
    repository = MetadataPostgresRepository(database_url)
    audience_key = f"coverage_{uuid4().hex}"
    timestamp = datetime(2026, 11, 1, tzinfo=UTC)
    digest = "a" * 64

    try:
        repository.check()
        repository.ensure_audience(audience_key, "1")
        initial = repository.get_snapshot_ref(audience_key)
        assert initial.snapshot_version == 0

        assert repository.link_tip(audience_key, 1, "1")
        assert not repository.link_tip(audience_key, 2_147_483_647, "1")
        assert repository.tip_links_for_audiences([audience_key])[audience_key][0].enabled
        assert repository.enabled_tip_ids([audience_key]) == {audience_key: [1]}
        assert repository.list_tips("review", 10, 0)[0].id == 1
        assert repository.list_tips(None, 1, 1)[0].id == 2

        first = repository.publish_snapshot(
            audience_key=audience_key,
            expected_version=0,
            new_version=1,
            object_key="bitmap:v1",
            manifest_key="manifest:v1",
            bitmap_sha256=digest,
            rule_version="1",
            source_watermark=timestamp,
            evaluated_at=timestamp,
        )
        assert first.snapshot_version == 1
        assert audience_key in {snapshot.audience_key for snapshot in repository.active_audiences()}

        unchanged = repository.publish_snapshot(
            audience_key=audience_key,
            expected_version=1,
            new_version=1,
            object_key="bitmap:v1",
            manifest_key="manifest:v1",
            bitmap_sha256=digest,
            rule_version="1",
            source_watermark=timestamp,
            evaluated_at=timestamp,
        )
        assert unchanged.snapshot_version == 1

        advanced = repository.publish_snapshot(
            audience_key=audience_key,
            expected_version=1,
            new_version=2,
            object_key="bitmap:v2",
            manifest_key="manifest:v2",
            bitmap_sha256="b" * 64,
            rule_version="2",
            source_watermark=timestamp,
            evaluated_at=timestamp,
        )
        assert advanced.snapshot_version == 2
        with pytest.raises(ConcurrentPublishError):
            repository.publish_snapshot(
                audience_key=audience_key,
                expected_version=1,
                new_version=2,
                object_key="bitmap:v2",
                manifest_key="manifest:v2",
                bitmap_sha256="b" * 64,
                rule_version="2",
                source_watermark=timestamp,
                evaluated_at=timestamp,
            )

        repository.unlink_tip(audience_key, 1)
        assert repository.enabled_tip_ids([audience_key]) == {audience_key: []}
        with repository.publish_job_lock(timeout_seconds=2):
            assert repository.get_snapshot_ref(audience_key).snapshot_version == 2
    finally:
        repository.delete_audience(audience_key)
