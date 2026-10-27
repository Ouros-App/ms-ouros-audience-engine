import asyncio
from contextlib import nullcontext
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import Mock, call

from fastapi.testclient import TestClient

from ouros_audience_engine.domain.config import AudienceConfig
from ouros_audience_engine.domain.tip_audience_catalog import (
    daily_water_meter_audience,
    default_tip_audiences,
)
from ouros_audience_engine.main import create_app
from ouros_audience_engine.services import audience_scheduler
from ouros_audience_engine.settings import Settings


def test_publish_cycle_reloads_mongo_and_publishes_only_enabled_definitions() -> None:
    enabled = daily_water_meter_audience()
    disabled = default_tip_audiences()[0]
    config = AudienceConfig(audiences=[enabled, disabled])
    definitions = Mock()
    definitions.load.return_value = config
    lock = Mock(return_value=nullcontext())
    publisher = Mock()
    publisher.publish.return_value = SimpleNamespace(
        audience_key=enabled.key,
        status="PUBLISHED",
        snapshot_version=1,
        member_count=40,
    )
    runtime = SimpleNamespace(
        api=SimpleNamespace(config=AudienceConfig(audiences=[]), definitions=definitions),
        metadata=SimpleNamespace(publish_job_lock=lock),
        publisher=publisher,
    )

    audience_scheduler.publish_enabled_audiences(runtime)

    assert runtime.api.config is config
    lock.assert_called_once_with()
    assert publisher.publish.call_args_list == [call(enabled)]


def test_periodic_runner_repeats_and_stops_cleanly(monkeypatch) -> None:
    call_count = 0
    two_cycles_finished = ThreadEvent()

    def publish(_runtime) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            two_cycles_finished.set()

    monkeypatch.setattr(audience_scheduler, "publish_enabled_audiences", publish)

    async def exercise() -> None:
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            audience_scheduler.run_periodically(
                SimpleNamespace(), stop_event, interval_seconds=0.02
            )
        )
        assert await asyncio.to_thread(two_cycles_finished.wait, 2)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())
    assert call_count == 2


def test_app_starts_and_stops_scheduler_with_lifespan(monkeypatch) -> None:
    started = ThreadEvent()
    stopped = ThreadEvent()

    async def wait_for_stop(_runtime, stop_event) -> None:
        started.set()
        await stop_event.wait()
        stopped.set()

    monkeypatch.setattr("ouros_audience_engine.main.run_periodically", wait_for_stop)
    runtime = SimpleNamespace(api=object(), redis_client=object(), mongo_client=object())

    with TestClient(create_app(Settings(), runtime)):
        assert started.wait(1)
    assert stopped.wait(1)
