from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientConnectionError

from ouros_audience_engine.adapters.redis_snapshot_store import SnapshotMissing
from ouros_audience_engine.api.internal import ApiServices
from ouros_audience_engine.domain.config import (
    AudienceConfig,
    AudienceDefinition,
    TipAudienceConfig,
    WaterAudienceConfig,
)
from ouros_audience_engine.domain.tip_audience_catalog import (
    daily_water_meter_audience,
    default_tip_audiences,
)
from ouros_audience_engine.main import create_app
from ouros_audience_engine.repositories.audience_definitions_mongo import (
    AudienceDefinitionConflict,
)
from ouros_audience_engine.settings import Settings

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_ISSUER = "https://issuer.example/realms/ouros"
_AUDIENCE = "ms-ouros-audience-engine"


class FakeDefinitions:
    def __init__(self) -> None:
        self.items: dict[str, AudienceDefinition] = {}

    def load(self) -> AudienceConfig:
        return AudienceConfig(audiences=list(self.items.values()))

    def list(self, search: str | None = None) -> list[AudienceDefinition]:
        return sorted(
            (
                definition
                for key, definition in self.items.items()
                if search is None or search.lower() in key.lower()
            ),
            key=lambda definition: definition.key,
        )

    def get(self, key: str) -> AudienceDefinition | None:
        return self.items.get(key)

    def create(self, definition: AudienceDefinition) -> None:
        if definition.key in self.items:
            raise AudienceDefinitionConflict(definition.key)
        self.items[definition.key] = definition

    def replace(self, key: str, definition: AudienceDefinition) -> bool:
        if key not in self.items:
            return False
        self.items[key] = definition
        return True

    def delete(self, key: str) -> bool:
        return self.items.pop(key, None) is not None


class FakeMetadata:
    def __init__(self) -> None:
        self.links: dict[str, dict[int, bool]] = {}
        self.tips = {10: "Hidrometro e leitura", 11: "Consumo de agua"}
        self.observations: dict[tuple[int, int], tuple[datetime, dict[str, object]]] = {}

    def upsert_tip_observation(
        self,
        farm_id: int,
        tip_id: int,
        observed_at: datetime,
        signals: dict[str, object],
    ) -> bool:
        self.observations[(farm_id, tip_id)] = (observed_at, signals)
        return True

    def enabled_tip_ids(self, keys: list[str]) -> dict[str, list[int]]:
        return {key: [10, 11] for key in keys}

    def get_snapshot_ref(self, key: str) -> SimpleNamespace:
        return SimpleNamespace(
            audience_key=key,
            snapshot_version=4,
            rule_version="1",
            source_watermark=datetime.now(UTC),
            evaluated_at=datetime.now(UTC),
        )

    def check(self) -> None:
        pass

    def publish_job_lock(self) -> AbstractContextManager[None]:
        return nullcontext()

    def list_tips(self, search: str | None, limit: int, offset: int) -> list[SimpleNamespace]:
        matches = [
            SimpleNamespace(id=tip_id, tip=tip)
            for tip_id, tip in self.tips.items()
            if search is None or search.lower() in tip.lower()
        ]
        return matches[offset : offset + limit]

    def tip_links_for_audiences(self, keys: list[str]) -> dict[str, list[SimpleNamespace]]:
        return {
            key: [
                SimpleNamespace(tip_id=tip_id, enabled=enabled)
                for tip_id, enabled in sorted(self.links.get(key, {}).items())
            ]
            for key in keys
        }

    def link_tip(
        self,
        audience_key: str,
        tip_id: int,
        rule_version: str,
        *,
        enabled: bool = True,
    ) -> bool:
        if tip_id not in self.tips:
            return False
        self.links.setdefault(audience_key, {})[tip_id] = enabled
        return True

    def unlink_tip(self, audience_key: str, tip_id: int) -> None:
        self.links.get(audience_key, {}).pop(tip_id, None)

    def delete_audience(self, audience_key: str) -> None:
        self.links.pop(audience_key, None)


class FakeReader:
    def __init__(
        self,
        members: set[int] | None = None,
        fail: bool = False,
        missing: bool = False,
    ) -> None:
        self.members = members or set()
        self.fail = fail
        self.missing = missing
        self.deleted_audiences: list[str] = []

    def get_bitmap(self, _snapshot: object) -> set[int]:
        if self.fail:
            raise OSError("snapshot unavailable")
        if self.missing:
            raise SnapshotMissing("snapshot missing")
        return self.members

    def delete_audience(self, audience_key: str) -> None:
        self.deleted_audiences.append(audience_key)


class FakePublisher:
    def __init__(self, reader: FakeReader) -> None:
        self.reader = reader
        self.calls = 0

    def publish(self, _audience: AudienceDefinition) -> None:
        self.calls += 1
        self.reader.members = {87}
        self.reader.missing = False


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


def client(
    reader: FakeReader,
    redis_client: object | None = None,
    publisher: FakePublisher | None = None,
    definitions: FakeDefinitions | None = None,
) -> TestClient:
    config = AudienceConfig(audiences=[audience("high_water_consumption")])
    metadata = FakeMetadata()
    services = ApiServices(
        metadata,
        reader,
        publisher or FakePublisher(reader),
        config,
        definitions,
    )
    runtime = SimpleNamespace(
        api=services,
        metadata=metadata,
        redis_client=redis_client or SimpleNamespace(ping=lambda: True),
    )
    settings = Settings(
        keycloak_issuer_url=_ISSUER,
        keycloak_audience=_AUDIENCE,
        keycloak_jwks_url="https://issuer.example/certs",
    )
    return TestClient(create_app(settings, runtime, enable_scheduler=False))


def _token(
    *,
    user_id: int | None = 87,
    audience: str = _AUDIENCE,
    issuer: str = _ISSUER,
    subject: str = "keycloak-user",
    account_type: str = "farm_owner",
    farm_id: int | None = 7,
    roles: list[str] | None = None,
    signing_key: object = _PRIVATE_KEY,
    expired: bool = False,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": issuer,
        "aud": audience,
        "exp": now - timedelta(minutes=5) if expired else now + timedelta(minutes=5),
        "iat": now,
        "sub": subject,
        "database_id": user_id,
        "account_type": account_type,
        "farm_id": farm_id,
        "realm_access": {"roles": roles if roles is not None else ["farm_owner"]},
    }
    return jwt.encode(claims, signing_key, algorithm="RS256")


def _install_test_jwks(monkeypatch, *, unavailable: bool = False) -> None:
    jwks = Mock()
    jwks.get_jwk_set.return_value = {}
    jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(key=_PUBLIC_KEY)
    if unavailable:
        jwks.get_jwk_set.side_effect = PyJWKClientConnectionError("offline")
    monkeypatch.setattr(
        "ouros_audience_engine.api.security._get_jwks_client",
        lambda _url: jwks,
    )


def _resolve(
    test_client: TestClient,
    token: str | None,
    user_id: int = 87,
    role: str = "FARM_OWNER",
):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return test_client.post(
        "/internal/v1/tips/resolve",
        json={"user_id": user_id, "user_role": role, "request_id": str(uuid4())},
        headers=headers,
    )


def test_resolution_requires_keycloak_farm_owner_token(monkeypatch) -> None:
    test_client = client(FakeReader({87}))
    assert _resolve(test_client, None).status_code == 401

    _install_test_jwks(monkeypatch)
    assert _resolve(test_client, _token(audience="wrong-audience")).status_code == 401
    assert _resolve(test_client, _token(issuer="https://wrong.example")).status_code == 401
    assert _resolve(test_client, _token(expired=True)).status_code == 401
    invalid_signature = _token(signing_key=_OTHER_PRIVATE_KEY)
    assert _resolve(test_client, invalid_signature).status_code == 401
    assert (
        _resolve(
            test_client,
            _token(account_type="company_employee", roles=["company_employee"]),
        ).status_code
        == 403
    )
    assert _resolve(test_client, _token(subject="")).status_code == 401
    assert _resolve(test_client, _token(farm_id=None)).status_code == 403
    assert _resolve(test_client, _token(), role="ADM").status_code == 422


def test_resolution_binds_body_identity_to_signed_keycloak_identity(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    test_client = client(FakeReader({87}))

    response = _resolve(test_client, _token())
    assert response.status_code == 200
    assert response.json()["user_id"] == 87
    assert response.json()["tip_ids"] == [10, 11]
    assert response.json()["snapshot_info"] == {"high_water_consumption": 4}
    assert _resolve(test_client, _token(), user_id=88).status_code == 403


def test_jwks_outage_returns_503(monkeypatch) -> None:
    _install_test_jwks(monkeypatch, unavailable=True)
    response = _resolve(client(FakeReader({87})), _token())
    assert response.status_code == 503


def test_snapshot_failure_returns_503_not_an_empty_tip_list(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    response = _resolve(client(FakeReader(fail=True)), _token())
    assert response.status_code == 503
    assert "tip_ids" not in response.json()


def test_resolution_rebuilds_missing_redis_snapshot(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    reader = FakeReader(missing=True)
    publisher = FakePublisher(reader)

    response = _resolve(client(reader, publisher=publisher), _token())

    assert response.status_code == 200
    assert response.json()["tip_ids"] == [10, 11]
    assert publisher.calls == 1


def test_readiness_requires_the_redis_snapshot_store() -> None:
    class UnavailableRedis:
        def ping(self) -> None:
            raise OSError("snapshot store unavailable")

    response = client(FakeReader(), UnavailableRedis()).get("/health/ready")

    assert response.status_code == 503


def test_farm_owner_can_only_read_their_own_audience_memberships(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    test_client = client(FakeReader({87}))
    headers = {"Authorization": f"Bearer {_token()}"}

    assert test_client.get("/internal/v1/users/87/audiences").status_code == 401

    own = test_client.get("/internal/v1/users/87/audiences", headers=headers)

    assert own.status_code == 200
    assert own.json()["user_id"] == 87
    assert own.json()["audiences"] == [
        {
            "key": "high_water_consumption",
            "version": "1",
            "tip_ids": [10, 11],
            "snapshot_version": 4,
        }
    ]

    other = test_client.get("/internal/v1/users/88/audiences", headers=headers)

    assert other.status_code == 403


def test_keycloak_admin_can_read_any_users_audience_memberships(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    test_client = client(FakeReader({999}))
    admin_token = _token(account_type="admin", user_id=None, farm_id=None, roles=["admin"])

    response = test_client.get(
        "/internal/v1/users/999/audiences",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == 999
    assert response.json()["audiences"][0]["key"] == "high_water_consumption"


def test_admin_can_crud_audiences_and_manage_tip_links(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    definitions = FakeDefinitions()
    reader = FakeReader()
    test_client = client(reader, definitions=definitions)
    admin_token = _token(
        user_id=None,
        account_type="admin",
        roles=["admin"],
    )
    admin = {"Authorization": f"Bearer {admin_token}"}
    payload = audience("water_saver").model_dump()

    created = test_client.post("/internal/v1/admin/audiences", json=payload, headers=admin)

    assert created.status_code == 201
    assert created.json()["definition"]["key"] == "water_saver"
    assert (
        test_client.post("/internal/v1/admin/audiences", json=payload, headers=admin).status_code
        == 409
    )
    assert (
        len(
            test_client.get(
                "/internal/v1/admin/audiences", params={"search": "water"}, headers=admin
            ).json()
        )
        == 1
    )
    assert test_client.get(
        "/internal/v1/admin/tips", params={"search": "agua"}, headers=admin
    ).json() == [{"id": 11, "tip": "Consumo de agua"}]

    link = test_client.put("/internal/v1/admin/audiences/water_saver/tips/10", headers=admin)
    assert link.status_code == 204
    assert test_client.get("/internal/v1/admin/audiences/water_saver", headers=admin).json()[
        "tips"
    ] == [{"tip_id": 10, "enabled": True}]
    assert (
        test_client.put(
            "/internal/v1/admin/audiences/water_saver/tips/999", headers=admin
        ).status_code
        == 404
    )

    updated = payload | {"version": "2", "activation_pct": 30}
    response = test_client.put(
        "/internal/v1/admin/audiences/water_saver", json=updated, headers=admin
    )
    assert response.status_code == 200
    assert response.json()["definition"]["version"] == "2"

    stale_version = updated | {"activation_pct": 35}
    assert (
        test_client.put(
            "/internal/v1/admin/audiences/water_saver",
            json=stale_version,
            headers=admin,
        ).status_code
        == 409
    )
    assert (
        test_client.delete(
            "/internal/v1/admin/audiences/water_saver/tips/10", headers=admin
        ).status_code
        == 204
    )
    assert (
        test_client.delete("/internal/v1/admin/audiences/water_saver", headers=admin).status_code
        == 204
    )
    assert reader.deleted_audiences == ["water_saver"]
    assert (
        test_client.get("/internal/v1/admin/audiences/water_saver", headers=admin).status_code
        == 404
    )


def test_only_keycloak_admin_can_manage_audiences(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    test_client = client(FakeReader(), definitions=FakeDefinitions())
    owner = {"Authorization": f"Bearer {_token()}"}

    assert test_client.get("/internal/v1/admin/audiences").status_code == 401
    assert test_client.get("/internal/v1/admin/audiences", headers=owner).status_code == 403
    assert (
        test_client.delete(
            "/internal/v1/admin/audiences/high_water_consumption", headers=owner
        ).status_code
        == 403
    )


def test_only_admin_can_upsert_complete_tip_observations(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    test_client = client(FakeReader(), definitions=FakeDefinitions())
    services = test_client.app.state.api_services
    metadata = FakeMetadata()
    services.metadata = metadata
    definition: TipAudienceConfig = default_tip_audiences()[0]
    services.config = AudienceConfig(audiences=[definition])
    path = f"/internal/v1/admin/farms/7/tips/{definition.tip_id}/observation"
    payload = {
        "observed_at": datetime.now(UTC).isoformat(),
        "signals": {
            "nipple_setup_out_of_age_target": True,
            "nipple_flow_ml_min": 75,
        },
    }
    admin = {
        "Authorization": f"Bearer {_token(user_id=None, account_type='admin', roles=['admin'])}"
    }
    owner = {"Authorization": f"Bearer {_token()}"}

    assert test_client.put(path, json=payload).status_code == 401
    assert test_client.put(path, json=payload, headers=owner).status_code == 403
    saved = test_client.put(path, json=payload, headers=admin)

    assert saved.status_code == 204
    assert metadata.observations[(7, definition.tip_id)][1] == payload["signals"]
    incomplete = payload | {"signals": {"nipple_flow_ml_min": 75}}
    assert test_client.put(path, json=incomplete, headers=admin).status_code == 422
    wrong_type = payload | {
        "signals": {
            "nipple_setup_out_of_age_target": 1,
            "nipple_flow_ml_min": 75,
        }
    }
    assert test_client.put(path, json=wrong_type, headers=admin).status_code == 422


def test_admin_can_create_and_link_a_tip_rule(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    definitions = FakeDefinitions()
    test_client = client(FakeReader(), definitions=definitions)
    admin = {
        "Authorization": f"Bearer {_token(user_id=None, account_type='admin', roles=['admin'])}"
    }
    definition = default_tip_audiences()[0].model_copy(update={"enabled": True})
    test_client.app.state.api_services.metadata.tips[definition.tip_id] = "Catalog tip"
    created = test_client.post(
        "/internal/v1/admin/audiences",
        json=definition.model_dump(),
        headers=admin,
    )
    assert created.status_code == 201
    linked = test_client.put(
        f"/internal/v1/admin/audiences/{definition.key}/tips/{definition.tip_id}",
        headers=admin,
    )

    assert linked.status_code == 204
    assert test_client.app.state.api_services.metadata.links[definition.key] == {
        definition.tip_id: True
    }


def test_daily_hydrometer_audience_can_link_only_its_configured_tip(monkeypatch) -> None:
    _install_test_jwks(monkeypatch)
    definitions = FakeDefinitions()
    test_client = client(FakeReader(), definitions=definitions)
    metadata = test_client.app.state.api_services.metadata
    definition = daily_water_meter_audience()
    definitions.create(definition)
    metadata.tips[definition.tip_id] = "Auditoria diária de hidrômetro"
    admin = {
        "Authorization": f"Bearer {_token(user_id=None, account_type='admin', roles=['admin'])}"
    }

    wrong_link = test_client.put(
        f"/internal/v1/admin/audiences/{definition.key}/tips/122",
        headers=admin,
    )
    correct_link = test_client.put(
        f"/internal/v1/admin/audiences/{definition.key}/tips/{definition.tip_id}",
        headers=admin,
    )

    assert wrong_link.status_code == 409
    assert correct_link.status_code == 204
    assert metadata.links[definition.key] == {definition.tip_id: True}
