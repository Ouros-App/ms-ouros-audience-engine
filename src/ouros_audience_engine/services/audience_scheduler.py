import asyncio
import hashlib
import json
import logging
from datetime import datetime
from threading import Event as ThreadEvent
from typing import cast

from pyroaring import BitMap

from ouros_audience_engine.domain.config import AudienceConfig
from ouros_audience_engine.runtime import Runtime
from ouros_audience_engine.services.audience_notifications import aggregate_memberships
from ouros_audience_engine.services.audience_publisher import PreparedPublication, PublishResult

logger = logging.getLogger(__name__)
PUBLISH_INTERVAL_SECONDS = 30 * 60
_PENDING_PUBLICATION_KEY = "ouros:notifications:publication:pending"


def publish_audience_cycle(
    runtime: Runtime,
    config: AudienceConfig | None = None,
    *,
    evaluated_at: datetime | None = None,
    stop_event: ThreadEvent | None = None,
) -> list[PublishResult]:
    """Prepare a whole candidate cycle in Redis before CAS, then resume its exact refs."""
    if stop_event is not None and stop_event.is_set():
        return []
    with runtime.metadata.publish_job_lock():
        raw_pending = runtime.redis_client.get(_PENDING_PUBLICATION_KEY)
        if raw_pending is None:
            if config is None:
                definitions = runtime.api.definitions
                if definitions is None:
                    raise RuntimeError("Mongo audience definitions are unavailable")
                config = definitions.load()
            if stop_event is not None and stop_event.is_set():
                return []
            runtime.api.config = config
            prepared: list[PreparedPublication] = []
            for audience in config.audiences:
                if not audience.enabled:
                    continue
                if stop_event is not None and stop_event.is_set():
                    return []
                result = runtime.publisher.publish(
                    audience,
                    evaluated_at=evaluated_at,
                    stop_event=stop_event,
                    prepare_only=True,
                )
                if not isinstance(result, PreparedPublication):
                    raise RuntimeError("publisher did not prepare an audience candidate")
                prepared.append(result)
            if stop_event is not None and stop_event.is_set():
                return []
            candidate = _candidate_cycle(runtime, config, prepared)
            encoded = json.dumps(candidate, separators=(",", ":"))
            if not runtime.redis_client.set(_PENDING_PUBLICATION_KEY, encoded, nx=True):
                raw_pending = runtime.redis_client.get(_PENDING_PUBLICATION_KEY)
                if raw_pending is None:
                    raise RuntimeError("pending audience cycle disappeared during staging")
            else:
                raw_pending = encoded

        cycle = json.loads(cast(str | bytes, raw_pending))
        prepared = [PreparedPublication.from_dict(item) for item in cycle["publications"]]
        # Recreate per-tip outbox records from the cycle's frozen candidate, never live snapshots.
        for tip in cycle["tips"]:
            runtime.notification_state.stage(
                int(tip["tip_id"]),
                str(tip["signature"]),
                set(map(int, tip["members"])),
                transition_id=str(tip["transition_id"]),
                revoked_ids=set(map(int, tip["revoked_ids"])),
            )
        results = [runtime.publisher.publish_prepared(item) for item in prepared]
        delivery_pending = False
        for tip in cycle["tips"]:
            if not runtime.notification_dispatcher.dispatch_pending(int(tip["tip_id"])):
                delivery_pending = True
        if delivery_pending:
            raise RuntimeError("Spring notification delivery remains pending")
        runtime.redis_client.delete(_PENDING_PUBLICATION_KEY)
        return results


def _candidate_cycle(
    runtime: Runtime,
    config: AudienceConfig,
    prepared: list[PreparedPublication],
) -> dict[str, object]:
    audiences = {audience.key: audience for audience in config.audiences}
    audience_keys = list(audiences)
    links_by_audience = runtime.metadata.tip_links_for_audiences(audience_keys)
    tips = []
    offset = 0
    while True:
        page = runtime.metadata.list_tips(None, limit=500, offset=offset)
        tips.extend(page)
        if len(page) < 500:
            break
        offset += len(page)
    linked: dict[int, list[str]] = {tip.id: [] for tip in tips}
    signatures: dict[int, str] = {}
    for tip_id in linked:
        links = sorted(
            (key, link.enabled, audiences[key].enabled, audiences[key].version)
            for key, audience_links in links_by_audience.items()
            for link in audience_links
            if link.tip_id == tip_id
        )
        signatures[tip_id] = hashlib.sha256(
            json.dumps(links, separators=(",", ":")).encode()
        ).hexdigest()
        linked[tip_id] = [
            key for key, is_linked, enabled, _version in links if is_linked and enabled
        ]
    bitmaps = {
        item.audience_key: BitMap(item.member_ids)
        for item in prepared
    }
    memberships = aggregate_memberships(bitmaps, linked)
    owner_ids = {
        owner_id for ids in runtime.source.load_farm_owners().values() for owner_id in ids
    }
    refs = sorted((item.audience_key, item.snapshot_version) for item in prepared)
    staged_tips = []
    for tip_id, members in memberships.items():
        raw_state = runtime.notification_state.redis_client.get(
            runtime.notification_state.state_key(tip_id)
        )
        previous_members: set[int] = set()
        if raw_state is not None:
            previous_members.update(map(int, json.loads(cast(str | bytes, raw_state))["members"]))
        revoked_ids = (previous_members | members) - owner_ids
        transition_id = hashlib.sha256(
            json.dumps([tip_id, signatures[tip_id], refs], separators=(",", ":")).encode()
        ).hexdigest()
        staged_tips.append(
            {
                "tip_id": tip_id,
                "signature": signatures[tip_id],
                "members": sorted(members),
                "revoked_ids": sorted(revoked_ids),
                "transition_id": transition_id,
            }
        )
    return {
        "publications": [item.to_dict() for item in prepared],
        "tips": staged_tips,
    }


def publish_enabled_audiences(
    runtime: Runtime,
    stop_event: ThreadEvent | None = None,
) -> None:
    results = publish_audience_cycle(runtime, stop_event=stop_event)
    for result in results:
        logger.info(
            "scheduled audience publication completed",
            extra={
                "audience_key": result.audience_key,
                "status": result.status,
                "snapshot_version": result.snapshot_version,
                "member_count": result.member_count,
            },
        )


async def run_periodically(
    runtime: Runtime,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = PUBLISH_INTERVAL_SECONDS,
    publication_stop_event: ThreadEvent | None = None,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    while not stop_event.is_set():
        try:
            if publication_stop_event is None:
                await asyncio.to_thread(publish_enabled_audiences, runtime)
            else:
                await asyncio.to_thread(
                    publish_enabled_audiences,
                    runtime,
                    publication_stop_event,
                )
        except Exception:
            logger.exception("scheduled audience refresh failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
