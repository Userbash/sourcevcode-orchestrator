from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]
    topic: str


class InventoryStreamHub:
    TOPIC_INVENTORY = "inventory"
    TOPIC_PROVIDER_INVENTORY = "provider_inventory"
    TOPIC_PROVIDER_RUNTIME_INVENTORY = "provider_runtime_inventory"
    TOPIC_MODEL_INDEX = "model_index"

    _SUPPORTED_TOPICS = {
        TOPIC_INVENTORY,
        TOPIC_PROVIDER_INVENTORY,
        TOPIC_PROVIDER_RUNTIME_INVENTORY,
        TOPIC_MODEL_INDEX,
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._version = 0
        self._published_at = 0
        self._snapshot: dict[str, Any] = self._normalize_snapshot({})
        self._topic_versions: dict[str, int] = {topic: 0 for topic in self._SUPPORTED_TOPICS}
        self._subscribers: list[_Subscriber] = []

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        raw = str(provider or "").strip().lower()
        if raw in {"google", "antigravity", "gemini"}:
            return "antigravity"
        if raw in {"local_llm", "ollama", "local"}:
            return "local_llm"
        if raw in {"ai-kernel", "ai_kernel", "llama_cpp", "llama-cpp"}:
            return "ai_kernel"
        if raw in {"mimo", "mimo-cli", "xiaomi", "github-copilot", "github-models"}:
            return "mimo"
        return raw

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        return str(model_name or "").strip()

    @classmethod
    def _normalize_snapshot(cls, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        payload = copy.deepcopy(snapshot if isinstance(snapshot, dict) else {})
        providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
        runtime_inventory = payload.get("runtime_inventory") if isinstance(payload.get("runtime_inventory"), dict) else {}
        runtime_providers = runtime_inventory.get("providers") if isinstance(runtime_inventory.get("providers"), dict) else {}
        model_index = payload.get("model_index") if isinstance(payload.get("model_index"), dict) else {}
        return {
            **payload,
            "updated_at": payload.get("updated_at"),
            "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {"provider_count": len(providers)},
            "providers": providers,
            "participation": payload.get("participation") if isinstance(payload.get("participation"), dict) else {},
            "provider_suppression": payload.get("provider_suppression") if isinstance(payload.get("provider_suppression"), dict) else {},
            "provider_budget_router": payload.get("provider_budget_router") if isinstance(payload.get("provider_budget_router"), dict) else {},
            "runtime_inventory": {
                "generated_at": runtime_inventory.get("generated_at") or payload.get("updated_at"),
                "providers": runtime_providers,
                "summary": runtime_inventory.get("summary") if isinstance(runtime_inventory.get("summary"), dict) else {},
            },
            "model_index": {
                "updated_at": model_index.get("updated_at", 0),
                "total_models": model_index.get("total_models", 0),
                "provider_counts": model_index.get("provider_counts", {}),
                "by_model": model_index.get("by_model", {}),
                "by_provider": model_index.get("by_provider", {}),
            },
        }

    @classmethod
    def _normalize_topic(cls, topic: str | None) -> str:
        raw = str(topic or cls.TOPIC_INVENTORY).strip().lower()
        aliases = {
            "all": cls.TOPIC_INVENTORY,
            "full": cls.TOPIC_INVENTORY,
            "inventory": cls.TOPIC_INVENTORY,
            "providers": cls.TOPIC_PROVIDER_INVENTORY,
            "provider_inventory": cls.TOPIC_PROVIDER_INVENTORY,
            "runtime": cls.TOPIC_PROVIDER_RUNTIME_INVENTORY,
            "runtime_inventory": cls.TOPIC_PROVIDER_RUNTIME_INVENTORY,
            "provider_runtime_inventory": cls.TOPIC_PROVIDER_RUNTIME_INVENTORY,
            "models": cls.TOPIC_MODEL_INDEX,
            "provider_models": cls.TOPIC_MODEL_INDEX,
            "models_index": cls.TOPIC_MODEL_INDEX,
            "model_index": cls.TOPIC_MODEL_INDEX,
        }
        normalized = aliases.get(raw, raw)
        if normalized not in cls._SUPPORTED_TOPICS:
            raise ValueError(f"unsupported inventory topic: {topic}")
        return normalized

    @staticmethod
    def _clone(value: Any) -> Any:
        return copy.deepcopy(value)

    def _topic_snapshot_locked(self, topic: str) -> dict[str, Any]:
        snapshot = self._snapshot if isinstance(self._snapshot, dict) else {}
        if topic == self.TOPIC_INVENTORY:
            return self._clone(snapshot)
        if topic == self.TOPIC_PROVIDER_INVENTORY:
            return {
                "updated_at": snapshot.get("updated_at"),
                "providers": self._clone(snapshot.get("providers", {})),
                "summary": self._clone(snapshot.get("summary", {})),
                "participation": self._clone(snapshot.get("participation", {})),
                "provider_suppression": self._clone(snapshot.get("provider_suppression", {})),
                "provider_budget_router": self._clone(snapshot.get("provider_budget_router", {})),
            }
        if topic == self.TOPIC_PROVIDER_RUNTIME_INVENTORY:
            runtime_inventory = snapshot.get("runtime_inventory") if isinstance(snapshot.get("runtime_inventory"), dict) else {}
            return {
                "generated_at": runtime_inventory.get("generated_at") or snapshot.get("updated_at"),
                "providers": self._clone(runtime_inventory.get("providers", {})),
                "summary": self._clone(runtime_inventory.get("summary", {})),
            }
        return self.model_index_summary()

    @staticmethod
    def _diff_mapping(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        upsert = {
            key: copy.deepcopy(value)
            for key, value in current.items()
            if key not in previous or previous.get(key) != value
        }
        remove = sorted(key for key in previous.keys() if key not in current)
        return {"upsert": upsert, "remove": remove}

    def _topic_delta(self, topic: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        if topic == self.TOPIC_PROVIDER_INVENTORY:
            return {
                "updated_at": current.get("updated_at"),
                "summary": self._clone(current.get("summary", {})),
                "providers": self._diff_mapping(
                    previous.get("providers", {}) if isinstance(previous.get("providers"), dict) else {},
                    current.get("providers", {}) if isinstance(current.get("providers"), dict) else {},
                ),
                "participation": self._clone(current.get("participation", {})),
                "provider_suppression": self._clone(current.get("provider_suppression", {})),
                "provider_budget_router": self._clone(current.get("provider_budget_router", {})),
            }
        if topic == self.TOPIC_PROVIDER_RUNTIME_INVENTORY:
            return {
                "generated_at": current.get("generated_at"),
                "summary": self._clone(current.get("summary", {})),
                "providers": self._diff_mapping(
                    previous.get("providers", {}) if isinstance(previous.get("providers"), dict) else {},
                    current.get("providers", {}) if isinstance(current.get("providers"), dict) else {},
                ),
            }
        if topic == self.TOPIC_MODEL_INDEX:
            return {
                "updated_at": current.get("updated_at", 0),
                "total_models": current.get("total_models", 0),
                "provider_counts": self._clone(current.get("provider_counts", {})),
                "by_provider": self._clone(current.get("by_provider", {})),
                "by_model": self._diff_mapping(
                    previous.get("by_model", {}) if isinstance(previous.get("by_model"), dict) else {},
                    current.get("by_model", {}) if isinstance(current.get("by_model"), dict) else {},
                ),
            }
        return {
            "updated_at": current.get("updated_at"),
            "topics": {
                self.TOPIC_PROVIDER_INVENTORY: self._topic_delta(
                    self.TOPIC_PROVIDER_INVENTORY,
                    self._extract_topic_snapshot(previous, self.TOPIC_PROVIDER_INVENTORY),
                    self._extract_topic_snapshot(current, self.TOPIC_PROVIDER_INVENTORY),
                ),
                self.TOPIC_PROVIDER_RUNTIME_INVENTORY: self._topic_delta(
                    self.TOPIC_PROVIDER_RUNTIME_INVENTORY,
                    self._extract_topic_snapshot(previous, self.TOPIC_PROVIDER_RUNTIME_INVENTORY),
                    self._extract_topic_snapshot(current, self.TOPIC_PROVIDER_RUNTIME_INVENTORY),
                ),
                self.TOPIC_MODEL_INDEX: self._topic_delta(
                    self.TOPIC_MODEL_INDEX,
                    self._extract_topic_snapshot(previous, self.TOPIC_MODEL_INDEX),
                    self._extract_topic_snapshot(current, self.TOPIC_MODEL_INDEX),
                ),
            },
        }

    def _extract_topic_snapshot(self, snapshot: dict[str, Any], topic: str) -> dict[str, Any]:
        preserved = self._snapshot
        try:
            self._snapshot = snapshot
            return self._topic_snapshot_locked(topic)
        finally:
            self._snapshot = preserved

    def _build_event_locked(self, topic: str, *, kind: str, snapshot: dict[str, Any], delta: dict[str, Any] | None) -> dict[str, Any]:
        event = {
            "topic": topic,
            "kind": kind,
            "version": int(self._topic_versions.get(topic, 0)),
            "global_version": int(self._version),
            "published_at": int(self._published_at),
            "snapshot": self._clone(snapshot),
            "data": self._clone(snapshot),
            "delta": self._clone(delta) if isinstance(delta, dict) else None,
            "topic_versions": self._clone(self._topic_versions),
        }
        if topic == self.TOPIC_INVENTORY:
            event["model_index"] = self._clone(snapshot.get("model_index", {}))
        return event

    def _event_locked(self) -> dict[str, Any]:
        return self._build_event_locked(
            self.TOPIC_INVENTORY,
            kind="snapshot",
            snapshot=self._topic_snapshot_locked(self.TOPIC_INVENTORY),
            delta=None,
        )

    def publish(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            previous_snapshot = self._clone(self._snapshot)
            previous_topics = {
                topic: self._extract_topic_snapshot(previous_snapshot, topic)
                for topic in self._SUPPORTED_TOPICS
            }
            self._version += 1
            self._published_at = int(time.time())
            self._snapshot = self._normalize_snapshot(snapshot)
            current_topics = {
                topic: self._topic_snapshot_locked(topic)
                for topic in self._SUPPORTED_TOPICS
            }
            events: dict[str, dict[str, Any]] = {}
            for topic in self._SUPPORTED_TOPICS:
                changed = topic == self.TOPIC_INVENTORY or previous_topics[topic] != current_topics[topic]
                if not changed:
                    continue
                self._topic_versions[topic] += 1
                events[topic] = self._build_event_locked(
                    topic,
                    kind="delta",
                    snapshot=current_topics[topic],
                    delta=self._topic_delta(topic, previous_topics[topic], current_topics[topic]),
                )
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            event = events.get(subscriber.topic)
            if event is None:
                continue
            try:
                subscriber.loop.call_soon_threadsafe(self._push_event, subscriber.queue, event)
            except RuntimeError:
                self.unsubscribe(subscriber.queue)
        return events.get(self.TOPIC_INVENTORY) or {
            "topic": self.TOPIC_INVENTORY,
            "kind": "delta",
            "version": int(self._topic_versions.get(self.TOPIC_INVENTORY, 0)),
            "global_version": int(self._version),
            "published_at": int(self._published_at),
            "snapshot": self._clone(self._snapshot),
            "data": self._clone(self._snapshot),
            "delta": None,
            "topic_versions": self._clone(self._topic_versions),
            "model_index": self.model_index_summary(),
        }

    @staticmethod
    def _push_event(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def snapshot(self, topic: str | None = None) -> dict[str, Any]:
        normalized_topic = self._normalize_topic(topic)
        with self._lock:
            return self._topic_snapshot_locked(normalized_topic)

    def current_event(self, topic: str | None = None) -> dict[str, Any]:
        normalized_topic = self._normalize_topic(topic)
        with self._lock:
            return self._build_event_locked(
                normalized_topic,
                kind="snapshot",
                snapshot=self._topic_snapshot_locked(normalized_topic),
                delta=None,
            )

    def current_version(self, topic: str | None = None) -> int:
        normalized_topic = self._normalize_topic(topic) if topic is not None else None
        with self._lock:
            if normalized_topic is None:
                return int(self._version)
            return int(self._topic_versions.get(normalized_topic, 0))

    def model_index_summary(self) -> dict[str, Any]:
        snapshot = self._snapshot if isinstance(self._snapshot, dict) else {}
        payload = snapshot.get("model_index") if isinstance(snapshot.get("model_index"), dict) else {}
        return {
            "updated_at": payload.get("updated_at", 0),
            "total_models": payload.get("total_models", 0),
            "provider_counts": self._clone(payload.get("provider_counts", {})),
            "by_model": self._clone(payload.get("by_model", {})),
            "by_provider": self._clone(payload.get("by_provider", {})),
        }

    def find_model(self, model_name: str) -> dict[str, Any] | None:
        key = self._normalize_model_name(model_name)
        if not key:
            return None
        row = self.model_index_summary().get("by_model", {}).get(key)
        return copy.deepcopy(row) if isinstance(row, dict) else None

    def provider_models(self, provider: str) -> list[str]:
        normalized = self._normalize_provider(provider)
        rows = self.model_index_summary().get("by_provider", {}).get(normalized, [])
        return list(rows) if isinstance(rows, list) else []

    def provider_runtime_entry(self, provider: str) -> dict[str, Any]:
        normalized = self._normalize_provider(provider)
        runtime_inventory = self.snapshot(self.TOPIC_PROVIDER_RUNTIME_INVENTORY)
        providers = runtime_inventory.get("providers") if isinstance(runtime_inventory.get("providers"), dict) else {}
        row = providers.get(normalized)
        return copy.deepcopy(row) if isinstance(row, dict) else {}

    def subscribe(
        self,
        topic: str | None = None,
        *,
        queue_maxsize: int = 1,
        include_snapshot: bool = True,
    ) -> tuple[asyncio.Queue[dict[str, Any]], dict[str, Any] | None]:
        normalized_topic = self._normalize_topic(topic)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, int(queue_maxsize or 1)))
        with self._lock:
            self._subscribers.append(_Subscriber(loop=loop, queue=queue, topic=normalized_topic))
            initial = None
            if include_snapshot:
                initial = self._build_event_locked(
                    normalized_topic,
                    kind="snapshot",
                    snapshot=self._topic_snapshot_locked(normalized_topic),
                    delta=None,
                )
        return queue, initial

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [subscriber for subscriber in self._subscribers if subscriber.queue is not queue]

    async def stream(
        self,
        topic: str | None = None,
        *,
        queue_maxsize: int = 1,
        include_snapshot: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        queue, initial = self.subscribe(topic, queue_maxsize=queue_maxsize, include_snapshot=include_snapshot)
        try:
            if initial is not None:
                yield initial
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(queue)
