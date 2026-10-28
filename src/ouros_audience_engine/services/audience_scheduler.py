import asyncio
import logging
from threading import Event as ThreadEvent

from ouros_audience_engine.runtime import Runtime

logger = logging.getLogger(__name__)
PUBLISH_INTERVAL_SECONDS = 30 * 60


def publish_enabled_audiences(
    runtime: Runtime,
    stop_event: ThreadEvent | None = None,
) -> None:
    definitions = runtime.api.definitions
    if definitions is None:
        raise RuntimeError("Mongo audience definitions are unavailable")
    if stop_event is not None and stop_event.is_set():
        return

    with runtime.metadata.publish_job_lock():
        if stop_event is not None and stop_event.is_set():
            return
        config = definitions.load()
        if stop_event is not None and stop_event.is_set():
            return
        runtime.api.config = config
        for audience in config.audiences:
            if stop_event is not None and stop_event.is_set():
                return
            if not audience.enabled:
                continue
            try:
                if stop_event is None:
                    result = runtime.publisher.publish(audience)
                else:
                    result = runtime.publisher.publish(audience, stop_event=stop_event)
            except InterruptedError:
                if stop_event is not None and stop_event.is_set():
                    return
                raise
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
