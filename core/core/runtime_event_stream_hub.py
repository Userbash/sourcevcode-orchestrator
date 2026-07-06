from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Subscription:
    subscription_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]
    dropped_events: int = 0


class RuntimeEventStreamHub:
    SCHEMA_VERSION = "runtime_events.ws.v1"
    STREAM_NAME = "runtime_events"

    def __init__(self, *, queue_size: int = 16, event_history_limit: int = 200) -> None:
        self._lock = threading.Lock()
        self._version = 0
        self._published_at = 0
        self._subscription_seq = 0
        self._queue_size = max(1, int(queue_size))
        self._event_history_limit = max(1, int(event_history_limit))
        self._state: dict[str, Any] = {
            "agents": {},
            "workflows": {},
            "events": [],
        }
        self._subscriptions: dict[str, _Subscription] = {}
        self._queue_index: dict[int, str] = {}

    def _copy_state_locked(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    def _next_subscription_id_locked(self) -> str:
        self._subscription_seq += 1
        return f"runtime-events-{self._subscription_seq}"

    def _snapshot_envelope_locked(self, *, subscription_id: str | None = None) -> dict[str, Any]:
        snapshot = self._copy_state_locked()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "stream": self.STREAM_NAME,
            "event": "snapshot",
            "subscription_id": subscription_id,
            "version": self._version,
            "published_at": self._published_at,
            "snapshot": snapshot,
            "delta": None,
            "dropped_events": 0,
            "final": False,
            "data": copy.deepcopy(snapshot),
        }

    def _delta_envelope_locked(
        self,
        *,
        previous_version: int,
        delta: dict[str, Any],
        subscription_id: str | None = None,
        dropped_events: int = 0,
    ) -> dict[str, Any]:
        snapshot = self._copy_state_locked()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "stream": self.STREAM_NAME,
            "event": "delta",
            "subscription_id": subscription_id,
            "version": self._version,
            "previous_version": previous_version,
            "published_at": self._published_at,
            "snapshot": snapshot,
            "delta": copy.deepcopy(delta),
            "dropped_events": int(dropped_events),
            "final": False,
            "data": copy.deepcopy(snapshot),
        }

    def _emit_to_subscribers(self, event: dict[str, Any], subscribers: list[_Subscription]) -> None:
        for subscription in subscribers:
            try:
                subscription.loop.call_soon_threadsafe(self._push_event, subscription, event)
            except RuntimeError:
                self.unsubscribe(subscription.subscription_id)

    def _trim_events_locked(self) -> None:
        events = self._state.setdefault("events", [])
        self._state["events"] = events[-self._event_history_limit :]

    def _publish_locked(self, delta: dict[str, Any]) -> tuple[dict[str, Any], list[_Subscription]]:
        previous_version = self._version
        self._version += 1
        self._published_at = int(time.time())
        event = self._delta_envelope_locked(previous_version=previous_version, delta=delta)
        return event, list(self._subscriptions.values())

    @staticmethod
    def _push_event(subscription: _Subscription, event: dict[str, Any]) -> None:
        queue = subscription.queue
        try:
            queue.put_nowait(dict(event, subscription_id=subscription.subscription_id, dropped_events=subscription.dropped_events))
            subscription.dropped_events = 0
            return
        except asyncio.QueueFull:
            pass

        dropped = 0
        while True:
            try:
                queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        subscription.dropped_events += max(1, dropped)
        try:
            queue.put_nowait(dict(event, subscription_id=subscription.subscription_id, dropped_events=subscription.dropped_events))
            subscription.dropped_events = 0
        except asyncio.QueueFull:
            pass

    def publish_agent_event(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = int(time.time())
            row = dict(self._state.get("agents", {}).get(agent_id, {}))
            row.update(dict(payload or {}))
            row["agent_id"] = agent_id
            row["updated_at"] = row.get("updated_at") or now
            self._state.setdefault("agents", {})[agent_id] = row
            event_row = {"kind": "agent", "agent_id": agent_id, **copy.deepcopy(row)}
            self._state.setdefault("events", []).append(event_row)
            self._trim_events_locked()
            event, subscribers = self._publish_locked({"kind": "agent", "agent_id": agent_id, "row": copy.deepcopy(row)})
        self._emit_to_subscribers(event, subscribers)
        return event

    def publish_workflow_event(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = int(time.time())
            row = dict(self._state.get("workflows", {}).get(workflow_id, {}))
            row.update(dict(payload or {}))
            row["workflow_id"] = workflow_id
            row["updated_at"] = row.get("updated_at") or now
            self._state.setdefault("workflows", {})[workflow_id] = row
            event_row = {"kind": "workflow", "workflow_id": workflow_id, **copy.deepcopy(row)}
            self._state.setdefault("events", []).append(event_row)
            self._trim_events_locked()
            event, subscribers = self._publish_locked({"kind": "workflow", "workflow_id": workflow_id, "row": copy.deepcopy(row)})
        self._emit_to_subscribers(event, subscribers)
        return event

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._copy_state_locked()

    def current_event(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_envelope_locked()

    def current_version(self) -> int:
        with self._lock:
            return int(self._version)

    def agent_snapshot(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._state.get("agents", {}).get(agent_id)
            return copy.deepcopy(row) if isinstance(row, dict) else {}

    def workflow_snapshot(self, workflow_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._state.get("workflows", {}).get(workflow_id)
            return copy.deepcopy(row) if isinstance(row, dict) else {}

    def subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "subscription_id": item.subscription_id,
                    "queue_size": item.queue.qsize(),
                    "queue_capacity": self._queue_size,
                    "dropped_events": item.dropped_events,
                }
                for item in self._subscriptions.values()
            ]

    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def subscribe(self) -> tuple[asyncio.Queue[dict[str, Any]], dict[str, Any]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            subscription_id = self._next_subscription_id_locked()
            subscription = _Subscription(subscription_id=subscription_id, loop=loop, queue=queue)
            self._subscriptions[subscription_id] = subscription
            self._queue_index[id(queue)] = subscription_id
            event = self._snapshot_envelope_locked(subscription_id=subscription_id)
        return queue, event

    def unsubscribe(self, queue_or_subscription: asyncio.Queue[dict[str, Any]] | str) -> bool:
        with self._lock:
            if isinstance(queue_or_subscription, str):
                subscription_id = queue_or_subscription
            else:
                subscription_id = self._queue_index.get(id(queue_or_subscription))
            if not subscription_id:
                return False
            subscription = self._subscriptions.pop(subscription_id, None)
            if subscription is None:
                return False
            self._queue_index.pop(id(subscription.queue), None)
            return True

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        queue, initial = self.subscribe()
        try:
            yield initial
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(queue)
