from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeAlias

EventPayload: TypeAlias = dict[str, object]
EventHandler: TypeAlias = Callable[[EventPayload], None]


class EventBus(Protocol):
    def publish(self, topic: str, payload: EventPayload) -> None: ...
    def subscribe(self, topic: str, handler: EventHandler) -> None: ...
