import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from ouros_audience_engine.domain.config import AudienceConfig
from ouros_audience_engine.domain.tip_audience_catalog import daily_water_meter_audience
from ouros_audience_engine.main import create_app
from ouros_audience_engine.services import audience_scheduler
from ouros_audience_engine.services.audience_publisher import PreparedPublication, PublishResult
from ouros_audience_engine.settings import Settings


def test_publish_scheduler_delegates_to_resumable_audience_cycle(monkeypatch) -> None:
    runtime = SimpleNamespace()
    cycle = Mock(return_value=[])
    monkeypatch.setattr(audience_scheduler, "publish_audience_cycle", cycle)

    audience_scheduler.publish_enabled_audiences(runtime)

    cycle.assert_called_once_with(runtime, stop_event=None)


def test_partial_publication_resumes_persisted_candidates_before_delivery() -> None:
    class MemoryRedis:
        def __init__(self, values: dict[str, bytes]) -> None:
            self.values = values

        def get(self, key: str) -> bytes | None:
            return self.values.get(key)

        def delete(self, key: str) -> None:
            self.values.pop(key, None)

    result = PublishResult("a", "UNCHANGED", 4, 1, 0, 1, 0, 0, 0, None)
    prepared = [
        PreparedPublication(key, 4, 4, None, None, "digest", "1", None, None, (42,), result, False)
        for key in ("a", "b")
    ]
    cycle_record = {
        "publications": [item.to_dict() for item in prepared],
        "tips": [
            {
                "tip_id": 9,
                "signature": "stable",
                "members": [42],
                "revoked_ids": [],
                "transition_id": "snapshots",
            }
        ],
    }
    key = audience_scheduler._PENDING_PUBLICATION_KEY
    redis_client = MemoryRedis({key: json.dumps(cycle_record).encode()})
    attempts: list[str] = []

    class Publisher:
        def publish(self, *_args, **_kwargs):
            raise AssertionError("resumption must not reevaluate candidates")

        def publish_prepared(self, item):
            attempts.append(item.audience_key)
            if attempts == ["a", "b"]:
                raise RuntimeError("partial CAS")
            return item.result

    @contextmanager
    def lock() -> Iterator[None]:
        yield

    runtime = SimpleNamespace(
        redis_client=redis_client,
        metadata=SimpleNamespace(publish_job_lock=lock),
        publisher=Publisher(),
        notification_state=SimpleNamespace(stage=Mock()),
        notification_dispatcher=SimpleNamespace(dispatch_pending=Mock(return_value=True)),
    )
    try:
        audience_scheduler.publish_audience_cycle(runtime)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the simulated partial publication must fail")
    runtime.notification_dispatcher.dispatch_pending.assert_not_called()
    assert redis_client.get(key) is not None

    audience_scheduler.publish_audience_cycle(runtime)

    assert attempts == ["a", "b", "a", "b"]
    runtime.notification_dispatcher.dispatch_pending.assert_called_once_with(9)
    assert redis_client.get(key) is None


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
        def publish(
            self,
            _audience,
            *,
            evaluated_at: datetime | None = None,
            stop_event: ThreadEvent,
            prepare_only: bool = False,
        ):
            assert prepare_only
            publication_started.set()
            assert stop_event.wait(1)
            publication_stopped.set()
            raise InterruptedError("publication stopped for shutdown")

    class ClosingClient:
        def get(self, _key: str):
            return None
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
