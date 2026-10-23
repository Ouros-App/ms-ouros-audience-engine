from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis
from pymongo import MongoClient

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_snapshot_store import RedisSnapshotStore
from ouros_audience_engine.api.internal import ApiServices
from ouros_audience_engine.domain.config import AudienceConfig
from ouros_audience_engine.repositories.audience_definitions_mongo import (
    AudienceDefinitionsMongoRepository,
)
from ouros_audience_engine.repositories.metadata_postgres import MetadataPostgresRepository
from ouros_audience_engine.repositories.source_postgres import SourcePostgresRepository
from ouros_audience_engine.services.audience_publisher import AudiencePublisher
from ouros_audience_engine.settings import Settings


@dataclass(frozen=True, slots=True)
class Runtime:
    api: ApiServices
    source: SourcePostgresRepository
    metadata: MetadataPostgresRepository
    snapshot_store: RedisSnapshotStore
    publisher: AudiencePublisher
    redis_client: redis.Redis
    mongo_client: MongoClient[dict[str, Any]]
    config: AudienceConfig


def build_runtime(settings: Settings) -> Runtime:
    mongo_client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=settings.source_timeout_seconds * 1000,
    )
    try:
        definitions = AudienceDefinitionsMongoRepository(
            mongo_client[settings.audience_database_name]["audience_definitions"]
        )
        config = definitions.load()
    except Exception:
        mongo_client.close()
        raise

    source = SourcePostgresRepository(settings.source_database_url, settings.source_timeout_seconds)
    metadata = MetadataPostgresRepository(
        settings.metadata_database_url, settings.source_timeout_seconds
    )
    redis_client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_connect_timeout=settings.source_timeout_seconds,
        socket_timeout=settings.source_timeout_seconds,
        health_check_interval=30,
    )
    codec = BitmapCodec()
    snapshot_store = RedisSnapshotStore(redis_client, codec)
    publisher = AudiencePublisher(source, metadata, snapshot_store, codec)
    api = ApiServices(metadata, snapshot_store, publisher, config, definitions)
    return Runtime(
        api, source, metadata, snapshot_store, publisher, redis_client, mongo_client, config
    )
