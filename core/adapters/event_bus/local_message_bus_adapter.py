from __future__ import annotations

from core.core.message_bus import MessageBus
from core.core.ports.event_bus import EventHandler, EventPayload


class LocalMessageBusAdapter:
    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    def publish(self, topic: str, payload: EventPayload) -> None:
        self.bus.publish(topic, payload)

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        self.bus.subscribe(topic, handler)
