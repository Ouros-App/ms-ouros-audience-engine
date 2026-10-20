from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from ouros_audience_engine.domain.config import AudienceConfig, WaterAudienceConfig
from ouros_audience_engine.main import create_app
from ouros_audience_engine.settings import Settings


class FakeMetadata:
    def enabled_tip_ids(self, keys: list[str]) -> dict[str, list[int]]:
        return {key: [10, 11] for key in keys}

    def get_snapshot_ref(self, key: str) -> SimpleNamespace:
        return SimpleNamespace(
            audience_key=key,
            snapshot_version=4,
            source_watermark=datetime.now(UTC),
            evaluated_at=datetime.now(UTC),
        )


class FakeReader:
    def __init__(self, members: set[int] | None = None, fail: bool = False) -> None:
        self.members = members or set()
        self.fail = fail

    def get_bitmap(self, _snapshot: object) -> set[int]:
        if self.fail:
            raise OSError("snapshot unavailable")
        return self.members


def audience(key: str) -> WaterAudienceConfig:
    return WaterAudienceConfig(
        key=key,
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


def client(reader: FakeReader) -> TestClient:
    config = AudienceConfig(audiences=[audience("high_water_consumption")])
    runtime = SimpleNamespace(
        metadata=FakeMetadata(),
        bitmap_reader=reader,
        config=config,
        redis_client=SimpleNamespace(),
        s3_client=SimpleNamespace(),
        s3_store=SimpleNamespace(bucket="test"),
    )
    settings = Settings(service_token="service-secret")
    return TestClient(create_app(settings, runtime))


def test_resolution_requires_service_token_and_validates_role() -> None:
    test_client = client(FakeReader({87}))
    payload = {"user_id": 87, "user_role": "FARM_OWNER", "request_id": str(uuid4())}
    assert test_client.post("/internal/v1/tips/resolve", json=payload).status_code == 401
    assert (
        test_client.post(
            "/internal/v1/tips/resolve",
            json={**payload, "user_role": "ADM"},
            headers={"Authorization": "Bearer service-secret"},
        ).status_code
        == 422
    )


def test_resolution_returns_only_enabled_tip_ids_for_matching_member() -> None:
    test_client = client(FakeReader({87}))
    response = test_client.post(
        "/internal/v1/tips/resolve",
        json={"user_id": 87, "user_role": "FARM_OWNER", "request_id": str(uuid4())},
        headers={"Authorization": "Bearer service-secret"},
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == 87
    assert response.json()["tip_ids"] == [10, 11]
    assert response.json()["snapshot_info"] == {"high_water_consumption": 4}


def test_snapshot_failure_returns_503_not_an_empty_tip_list() -> None:
    test_client = client(FakeReader(fail=True))
    response = test_client.post(
        "/internal/v1/tips/resolve",
        json={"user_id": 87, "user_role": "FARM_OWNER", "request_id": str(uuid4())},
        headers={"Authorization": "Bearer service-secret"},
    )
    assert response.status_code == 503
    assert "tip_ids" not in response.json()
