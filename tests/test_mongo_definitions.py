from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from ouros_audience_engine.domain.config import WaterAudienceConfig
from ouros_audience_engine.repositories.audience_definitions_mongo import (
    AudienceDefinitionConflict,
    AudienceDefinitionsMongoRepository,
)


def definition(key: str, **changes: Any) -> dict[str, Any]:
    return {
        "key": key,
        "version": "1",
        "enabled": True,
        "subject": "farm",
        "member_id_space": "farm_owner_user_id",
        "source": "postgres",
        "metric": "normalized_water_consumption",
        "period": "last_complete_month",
        "baseline": "previous_consecutive_months",
        "activation_pct": 25,
        "resolution_pct": 15,
        "min_baseline_samples": 3,
        "max_observation_age_days": 45,
        "unknown_policy": "preserve_previous_membership",
        "first_run_push": False,
        **changes,
    }


class FakeCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def sort(self, field: str, direction: int) -> FakeCursor:
        self.documents.sort(key=lambda document: document[field], reverse=direction < 0)
        return self

    def __iter__(self):
        return iter(self.documents)


class FakeCollection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def find(
        self, query: dict[str, Any], projection: dict[str, bool]
    ) -> FakeCursor:
        expression = query.get("key", {}).get("$regex")
        options = query.get("key", {}).get("$options")
        documents = [
            {key: value for key, value in document.items() if projection.get(key, True)}
            for document in self.documents
            if expression is None
            or re.search(expression, document["key"], re.IGNORECASE if options == "i" else 0)
        ]
        return FakeCursor(documents)

    def find_one(
        self, query: dict[str, Any], projection: dict[str, bool]
    ) -> dict[str, Any] | None:
        document = next(
            (document for document in self.documents if document["key"] == query["key"]),
            None,
        )
        if document is None:
            return None
        return {key: value for key, value in document.items() if projection.get(key, True)}

    def insert_one(self, document: dict[str, Any]) -> None:
        if any(item["key"] == document["key"] for item in self.documents):
            raise DuplicateKeyError("duplicate key")
        self.documents.append(document.copy())

    def replace_one(
        self, query: dict[str, Any], document: dict[str, Any], *, upsert: bool
    ) -> SimpleNamespace:
        existing = self.find_one(query, {"_id": False})
        if existing is None:
            return SimpleNamespace(matched_count=0)
        index = next(
            index for index, item in enumerate(self.documents) if item["key"] == query["key"]
        )
        self.documents[index] = document.copy()
        return SimpleNamespace(matched_count=1)

    def delete_one(self, query: dict[str, Any]) -> SimpleNamespace:
        for index, document in enumerate(self.documents):
            if document["key"] == query["key"]:
                del self.documents[index]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


def test_repository_searches_and_mutates_definitions() -> None:
    collection = FakeCollection([])
    repository = AudienceDefinitionsMongoRepository(collection)
    water_saver = WaterAudienceConfig.model_validate(definition("water_saver"))

    repository.create(water_saver)

    assert [item.key for item in repository.list("WATER")] == ["water_saver"]
    assert repository.get("water_saver") == water_saver
    with pytest.raises(AudienceDefinitionConflict):
        repository.create(water_saver)

    revised = water_saver.model_copy(update={"version": "2"})
    assert repository.replace("water_saver", revised)
    assert repository.get("water_saver") == revised
    assert not repository.replace("missing", revised)
    assert repository.delete("water_saver")
    assert not repository.delete("water_saver")


def test_load_validates_and_orders_mongo_definitions() -> None:
    collection = FakeCollection(
        [definition("second_audience", _id="mongo-2"), definition("first_audience", _id="mongo-1")]
    )

    config = AudienceDefinitionsMongoRepository(collection).load()

    assert [audience.key for audience in config.audiences] == [
        "first_audience",
        "second_audience",
    ]


def test_load_rejects_invalid_rule_documents() -> None:
    collection = FakeCollection([definition("invalid_audience", resolution_pct=25)])

    with pytest.raises(ValidationError, match="activation_pct must be greater"):
        AudienceDefinitionsMongoRepository(collection).load()
