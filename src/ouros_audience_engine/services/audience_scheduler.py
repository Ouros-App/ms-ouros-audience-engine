import asyncio
import logging

from ouros_audience_engine.runtime import Runtime

logger = logging.getLogger(__name__)
PUBLISH_INTERVAL_SECONDS = 30 * 60


def publish_enabled_audiences(runtime: Runtime) -> None:
    definitions = runtime.api.definitions
    if definitions is None:
        raise RuntimeError("Mongo audience definitions are unavailable")

    with runtime.metadata.publish_job_lock():
        config = definitions.load()
        runtime.api.config = config
        for audience in config.audiences:
            if not audience.enabled:
                continue
            try:
                result = runtime.publisher.publish(audience)
            except Exception:
                logger.exception(
                    "scheduled audience publication failed",
                    extra={"audience_key": audience.key},
                )
            else:
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
) -> None:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(publish_enabled_audiences, runtime)
        except Exception:
            logger.exception("scheduled audience refresh failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
