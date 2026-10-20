from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3
import redis
from botocore.config import Config as BotoConfig

from ouros_audience_engine.adapters.bitmap_codec import BitmapCodec
from ouros_audience_engine.adapters.redis_cache import BitmapReader
from ouros_audience_engine.adapters.s3_store import S3Store
from ouros_audience_engine.api.internal import ApiServices
from ouros_audience_engine.domain.config import AudienceConfig, load_audience_config
from ouros_audience_engine.repositories.metadata_postgres import MetadataPostgresRepository
from ouros_audience_engine.repositories.source_postgres import SourcePostgresRepository
from ouros_audience_engine.services.audience_publisher import AudiencePublisher
from ouros_audience_engine.settings import Settings


@dataclass(frozen=True, slots=True)
class Runtime:
    api: ApiServices
    source: SourcePostgresRepository
    metadata: MetadataPostgresRepository
    s3_store: S3Store
    bitmap_reader: BitmapReader
    publisher: AudiencePublisher
    redis_client: redis.Redis
    s3_client: Any
    config: AudienceConfig


def build_runtime(settings: Settings) -> Runtime:
    config = load_audience_config(settings.audience_config_path)
    source = SourcePostgresRepository(settings.source_database_url, settings.source_timeout_seconds)
    metadata = MetadataPostgresRepository(
        settings.metadata_database_url, settings.source_timeout_seconds
    )
    s3_client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id or None,
        aws_secret_access_key=settings.s3_secret_access_key or None,
        use_ssl=settings.s3_use_ssl,
        config=BotoConfig(
            connect_timeout=settings.source_timeout_seconds,
            read_timeout=settings.source_timeout_seconds,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    store = S3Store(s3_client, settings.s3_bucket, settings.s3_prefix)
    redis_client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_connect_timeout=settings.source_timeout_seconds,
        socket_timeout=settings.source_timeout_seconds,
        health_check_interval=30,
    )
    codec = BitmapCodec()
    reader = BitmapReader(
        redis_client,
        store,
        codec,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    publisher = AudiencePublisher(source, metadata, store, codec, reader)
    api = ApiServices(metadata, reader, config, settings.service_token)
    return Runtime(api, source, metadata, store, reader, publisher, redis_client, s3_client, config)
