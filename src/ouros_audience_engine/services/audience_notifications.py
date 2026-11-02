from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast
from urllib.error import URLError
from urllib.request import Request, urlopen

import redis
from pyroaring import BitMap

ACTIVATED_TITLE = "Nova Dica Técnica!"
ACTIVATED_MESSAGE = "Uma nova dica técnica está disponível para você."
RESOLVED_TITLE = "Dica resolvida"
RESOLVED_MESSAGE = "Uma dica técnica foi resolvida para você."
MAX_BATCH_SIZE = 500


class AudienceNotificationState:
    """Redis-only baseline and pending membership cycle for audience transitions."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis_client = redis_client

    @staticmethod
    def state_key(tip_id: int) -> str:
        return f"ouros:notifications:tip:{tip_id}:state"

    @staticmethod
    def pending_key(tip_id: int) -> str:
        return f"ouros:notifications:tip:{tip_id}:pending"

    def stage(
        self,
        tip_id: int,
        signature: str,
        members: set[int],
        transition_id: str = "",
        revoked_ids: set[int] | None = None,
    ) -> tuple[set[int], set[int]]:
        """Persist a retryable membership transition before delivery."""
        state_key = self.state_key(tip_id)
        pending_key = self.pending_key(tip_id)
        pending = self.redis_client.get(pending_key)
        if pending is not None:
            saved = json.loads(cast(str | bytes, pending))
            if saved["signature"] != signature or saved.get("transition_id", "") != transition_id:
                raise RuntimeError("cannot replace an unacknowledged notification cycle")
            return set(saved["added"]), set(saved["removed"])

        raw_state = self.redis_client.get(state_key)
        previous: set[int] = set()
        bootstrap = raw_state is None
        if raw_state is not None:
            state: dict[str, Any] = json.loads(cast(str | bytes, raw_state))
            previous = set(state["members"])

        added = (members - previous) - (revoked_ids or set()) if not bootstrap else set()
        removed = (previous - members) - (revoked_ids or set()) if not bootstrap else set()
        cycle_id = hashlib.sha256(
            f"{tip_id}:{signature}:{transition_id}:{sorted(previous)}:{sorted(members)}".encode()
        ).hexdigest()
        payload = json.dumps(
            {
                "signature": signature,
                "transition_id": transition_id,
                "members": sorted(members),
                "added": sorted(added),
                "removed": sorted(removed),
                "cycle_id": cycle_id,
            },
            separators=(",", ":"),
        )
        self.redis_client.set(pending_key, payload, nx=True)
        return added, removed

    def acknowledge(self, tip_id: int) -> None:
        pending_key = self.pending_key(tip_id)
        raw_pending = self.redis_client.get(pending_key)
        if raw_pending is None:
            return
        pending = json.loads(cast(str | bytes, raw_pending))
        baseline = {"signature": pending["signature"], "members": pending["members"]}
        self.redis_client.set(
            self.state_key(tip_id),
            json.dumps(baseline, separators=(",", ":")),
        )
        self.redis_client.delete(pending_key)

    def load_pending(self, tip_id: int) -> dict[str, Any] | None:
        raw = self.redis_client.get(self.pending_key(tip_id))
        return json.loads(cast(str | bytes, raw)) if raw is not None else None


class SpringNotificationDispatcher:
    def __init__(
        self,
        state: AudienceNotificationState,
        base_url: str,
        service_key: str,
        timeout_seconds: int,
    ) -> None:
        self.state = state
        self.url = f"{base_url.rstrip('/')}/internal/v1/notifications/batch"
        self.service_key = service_key.strip()
        self.timeout_seconds = timeout_seconds

    def dispatch_pending(self, tip_id: int) -> bool:
        pending = self.state.load_pending(tip_id)
        if pending is None:
            return True
        cycle_id = str(pending["cycle_id"])
        notifications = [
            self._notification(tip_id, int(user_id), "TIP_ACTIVATED", cycle_id)
            for user_id in pending["added"]
        ] + [
            self._notification(tip_id, int(user_id), "TIP_RESOLVED", cycle_id)
            for user_id in pending["removed"]
        ]
        if not notifications:
            self.state.acknowledge(tip_id)
            return True
        if not self.service_key:
            return False
        for start in range(0, len(notifications), MAX_BATCH_SIZE):
            batch = notifications[start : start + MAX_BATCH_SIZE]
            request = Request(
                self.url,
                data=json.dumps({"notifications": batch}, separators=(",", ":")).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Service-Key": self.service_key,
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    result = json.loads(response.read())
            except (URLError, TimeoutError, ValueError, OSError):
                return False
            if not isinstance(result, dict):
                return False
            received = result.get("total_received")
            created = result.get("total_created")
            skipped = result.get("total_skipped")
            if (
                isinstance(received, bool)
                or isinstance(created, bool)
                or isinstance(skipped, bool)
                or not isinstance(received, int)
                or not isinstance(created, int)
                or not isinstance(skipped, int)
                or received != len(batch)
                or created < 0
                or skipped < 0
                or created + skipped != received
            ):
                return False
        self.state.acknowledge(tip_id)
        return True

    @staticmethod
    def _notification(
        tip_id: int,
        user_id: int,
        event_type: str,
        cycle_id: str,
    ) -> dict[str, object]:
        activated = event_type == "TIP_ACTIVATED"
        event_id = hashlib.sha256(
            f"{cycle_id}:{tip_id}:{user_id}:{event_type}".encode()
        ).hexdigest()
        return {
            "event_id": event_id,
            "user_id": user_id,
            "user_role": "FARM_OWNER",
            "title": ACTIVATED_TITLE if activated else RESOLVED_TITLE,
            "message": ACTIVATED_MESSAGE if activated else RESOLVED_MESSAGE,
            "type": event_type,
            "id_tip": tip_id,
            "data_payload": json.dumps({"id_tip": tip_id}, separators=(",", ":")),
            "send_push": activated,
        }


def aggregate_memberships(
    bitmaps: Mapping[str, BitMap],
    linked_audiences: Mapping[int, list[str]],
) -> dict[int, set[int]]:
    """Union memberships across all enabled linked audiences per tip."""
    result: dict[int, set[int]] = {}
    for tip_id, audience_keys in linked_audiences.items():
        members: set[int] = set()
        for key in audience_keys:
            bitmap = bitmaps.get(key)
            if bitmap is not None:
                members.update(bitmap)
        result[tip_id] = members
    return result
