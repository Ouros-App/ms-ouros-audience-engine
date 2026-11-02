import json

from pyroaring import BitMap

from ouros_audience_engine.services.audience_notifications import (
    AudienceNotificationState,
    SpringNotificationDispatcher,
    aggregate_memberships,
)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def set(self, key: str, value: str, nx: bool = False) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value.encode()
        return True

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_memberships_union_across_linked_audiences() -> None:
    actual = aggregate_memberships(
        {"one": BitMap([1, 2]), "two": BitMap([2, 3])},
        {9: ["one", "two"], 10: ["one"]},
    )
    assert actual == {9: {1, 2, 3}, 10: {1, 2}}


def test_cycle_bootstraps_silently_and_preserves_pending_until_ack() -> None:
    redis = MemoryRedis()
    state = AudienceNotificationState(redis)  # type: ignore[arg-type]

    assert state.stage(7, "sig-a", {1, 2}) == (set(), set())
    pending = state.load_pending(7)
    assert pending is not None
    assert pending["added"] == []
    assert state.stage(7, "sig-a", {9}) == (set(), set())

    state.acknowledge(7)
    assert state.load_pending(7) is None
    assert json.loads(redis.get(state.state_key(7))) == {"signature": "sig-a", "members": [1, 2]}
    assert state.stage(7, "sig-a", {2, 3}) == ({3}, {1})


def test_signature_change_preserves_membership_delta_and_pending_cycle() -> None:
    redis = MemoryRedis()
    state = AudienceNotificationState(redis)  # type: ignore[arg-type]
    redis.set(state.state_key(4), json.dumps({"signature": "old", "members": [1]}))

    assert state.stage(4, "new", {2}) == ({2}, {1})
    try:
        state.stage(4, "changed-again", {3})
    except RuntimeError as error:
        assert "unacknowledged" in str(error)
    else:
        raise AssertionError("pending cycle was overwritten")


def test_pending_cycle_rejects_different_transition_id() -> None:
    redis = MemoryRedis()
    state = AudienceNotificationState(redis)  # type: ignore[arg-type]
    state.stage(5, "same-signature", {1}, "version-1")

    try:
        state.stage(5, "same-signature", {2}, "version-2")
    except RuntimeError as error:
        assert "unacknowledged" in str(error)
    else:
        raise AssertionError("pending cycle was replaced by a newer transition")


def test_dispatch_retries_with_stable_ids_and_validates_ack(monkeypatch) -> None:
    redis = MemoryRedis()
    state = AudienceNotificationState(redis)  # type: ignore[arg-type]
    state.stage(12, "signature", {1}, "snapshot-1")
    state.acknowledge(12)
    state.stage(12, "signature", {1, 2}, "snapshot-2")
    sent: list[dict[str, object]] = []
    response_counts = [{"total_received": 1, "total_created": 0, "total_skipped": 0}]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(response_counts[0]).encode()

    def send(request, timeout):
        assert timeout == 3
        sent.append(json.loads(request.data)["notifications"][0])
        return Response()

    monkeypatch.setattr(
        "ouros_audience_engine.services.audience_notifications.urlopen", send
    )
    dispatcher = SpringNotificationDispatcher(state, "https://example.invalid", "secret", 3)

    assert not dispatcher.dispatch_pending(12)
    response_counts[0] = {"total_received": 1, "total_created": 1, "total_skipped": 0}
    assert dispatcher.dispatch_pending(12)
    assert sent[0]["event_id"] == sent[1]["event_id"]
    assert sent[0]["type"] == "TIP_ACTIVATED"
    assert sent[0]["send_push"] is True
    assert json.loads(sent[0]["data_payload"]) == {"id_tip": 12}
    assert state.load_pending(12) is None


def test_dispatch_without_key_keeps_pending_and_resolution_is_in_app() -> None:
    redis = MemoryRedis()
    state = AudienceNotificationState(redis)  # type: ignore[arg-type]
    state.stage(12, "signature", {1}, "snapshot-1")
    state.acknowledge(12)
    state.stage(12, "signature", set(), "snapshot-2")

    dispatcher = SpringNotificationDispatcher(state, "https://example.invalid", "", 1)
    assert not dispatcher.dispatch_pending(12)
    assert state.load_pending(12) is not None

    event = dispatcher._notification(12, 1, "TIP_RESOLVED", "cycle")
    assert event["send_push"] is False
    assert event["title"] == "Dica resolvida"


def test_cycle_suppresses_access_revocations() -> None:
    redis = MemoryRedis()
    state = AudienceNotificationState(redis)  # type: ignore[arg-type]
    state.stage(20, "signature", {4, 5}, "snapshot-1")
    state.acknowledge(20)

    assert state.stage(20, "signature", {4}, "snapshot-2", {5}) == (set(), set())
    state.stage(21, "signature", {4}, "snapshot-1")
    state.acknowledge(21)
    assert state.stage(21, "signature", {4, 5}, "snapshot-2", {5}) == (set(), set())


def test_dispatch_limits_requests_to_five_hundred_notifications(monkeypatch) -> None:
    redis = MemoryRedis()
    state = AudienceNotificationState(redis)  # type: ignore[arg-type]
    state.stage(21, "signature", set(), "snapshot-0")
    state.acknowledge(21)
    state.stage(21, "signature", set(range(1, 502)), "snapshot-1")
    batch_sizes: list[int] = []

    class Response:
        def __init__(self, received: int) -> None:
            self.received = received

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "total_received": self.received,
                    "total_created": self.received,
                    "total_skipped": 0,
                }
            ).encode()

    def send(request, **_kwargs):
        batch = json.loads(request.data)["notifications"]
        batch_sizes.append(len(batch))
        return Response(len(batch))

    monkeypatch.setattr(
        "ouros_audience_engine.services.audience_notifications.urlopen", send
    )

    assert SpringNotificationDispatcher(
        state, "https://example.invalid", "secret", 1
    ).dispatch_pending(21)
    assert batch_sizes == [500, 1]
