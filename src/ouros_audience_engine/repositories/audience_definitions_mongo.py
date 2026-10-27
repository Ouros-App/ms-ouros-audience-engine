import re
from typing import Any

from pydantic import TypeAdapter
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from ouros_audience_engine.domain.config import (
    AudienceConfig,
    AudienceDefinition,
)

_AUDIENCE_ADAPTER: TypeAdapter[AudienceDefinition] = TypeAdapter(AudienceDefinition)


class AudienceDefinitionConflict(RuntimeError):
    pass


class AudienceDefinitionsMongoRepository:
    def __init__(self, collection: Collection[dict[str, Any]]) -> None:
        self.collection = collection

    def load(self) -> AudienceConfig:
        documents = list(self.collection.find({}, {"_id": False}).sort("key", 1))
        return AudienceConfig.model_validate({"audiences": documents})

    def list(self, search: str | None = None) -> list[AudienceDefinition]:
        query = {"key": {"$regex": re.escape(search), "$options": "i"}} if search else {}
        documents = self.collection.find(query, {"_id": False}).sort("key", 1)
        return [_AUDIENCE_ADAPTER.validate_python(document) for document in documents]

    def get(self, key: str) -> AudienceDefinition | None:
        document = self.collection.find_one({"key": key}, {"_id": False})
        return _AUDIENCE_ADAPTER.validate_python(document) if document is not None else None

    def create(self, definition: AudienceDefinition) -> None:
        try:
            self.collection.insert_one(definition.model_dump())
        except DuplicateKeyError as exc:
            raise AudienceDefinitionConflict(definition.key) from exc

    def replace(self, key: str, definition: AudienceDefinition) -> bool:
        return (
            self.collection.replace_one(
                {"key": key}, definition.model_dump(), upsert=False
            ).matched_count
            == 1
        )

    def delete(self, key: str) -> bool:
        return self.collection.delete_one({"key": key}).deleted_count == 1
