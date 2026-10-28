import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import Mock, call

from fastapi.testclient import TestClient

from ouros_audience_engine.domain.config import AudienceConfig, AudienceDefinition
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
    lock_active = False

    @contextmanager
    def publish_lock() -> Iterator[None]:
        nonlocal lock_active
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    def load() -> AudienceConfig:
        assert lock_active
        return config

    def publish(_audience: AudienceDefinition) -> SimpleNamespace:
        assert lock_active
        return SimpleNamespace(
            audience_key=enabled.key,
            status="PUBLISHED",
            snapshot_version=1,
            member_count=40,
        )

    definitions = Mock()
    definitions.load.side_effect = load
    lock = Mock(side_effect=publish_lock)
    publisher = Mock()
    publisher.publish.side_effect = publish
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

    async def wait_for_stop(
        _runtime,
        stop_event,
        *,
        publication_stop_event: ThreadEvent,
    ) -> None:
        started.set()
        await stop_event.wait()
        assert publication_stop_event.is_set()
        stopped.set()

    monkeypatch.setattr("ouros_audience_engine.main.run_periodically", wait_for_stop)
    runtime = SimpleNamespace(api=object(), redis_client=object(), mongo_client=object())

    with TestClient(create_app(Settings(), runtime)):
        assert started.wait(1)
    assert stopped.wait(1)


def test_app_shutdown_stops_an_in_flight_publication(monkeypatch) -> None:
    publication_started = ThreadEvent()
    publication_stopped = ThreadEvent()
    close_calls: list[bool] = []
    audience = daily_water_meter_audience()
    config = AudienceConfig(audiences=[audience])

    @contextmanager
    def publish_lock() -> Iterator[None]:
        yield

    class Publisher:
        def publish(self, _audience, *, stop_event: ThreadEvent):
            publication_started.set()
            assert stop_event.wait(1)
            publication_stopped.set()
            raise InterruptedError("publication stopped for shutdown")

    class ClosingClient:
        def close(self) -> None:
            assert publication_stopped.is_set()
            close_calls.append(True)

    runtime = SimpleNamespace(
        api=SimpleNamespace(
            config=AudienceConfig(audiences=[]),
            definitions=SimpleNamespace(load=lambda: config),
        ),
        metadata=SimpleNamespace(publish_job_lock=publish_lock),
        publisher=Publisher(),
        redis_client=ClosingClient(),
        mongo_client=ClosingClient(),
    )
    monkeypatch.setattr(
        "ouros_audience_engine.main.build_runtime",
        lambda _settings: runtime,
    )

    with TestClient(create_app(Settings())):
        assert publication_started.wait(1)

    assert publication_stopped.is_set()
    assert len(close_calls) == 2
