from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
from collections import defaultdict
from dataclasses import asdict
from collections.abc import Callable
from typing import Any

from .message_bus import MessageBus
from .models import AckStatus, MessageAck, P2PMessage, TaskEnvelope

logger = logging.getLogger(__name__)

try:
    import aio_pika
except Exception:  # pragma: no cover
    aio_pika = None  # type: ignore[assignment]


class RabbitMQBus(MessageBus):
    def __init__(self, url: str | None = None) -> None:
        super().__init__()
        self.url = (url or os.getenv("AI_BRIDGE_RABBITMQ_URL", "amqp://guest:guest@localhost/")).strip()
        self._connection: Any = None
        self._channel: Any = None
        self._direct_exchange: Any = None
        self._topic_exchange: Any = None
        self._topic_callbacks: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._consumer_tasks: list[asyncio.Task[Any]] = []
        self._direct_queues_ready: set[str] = set()
        self._topic_queues_ready: set[str] = set()
        self._enabled = aio_pika is not None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        if not self._enabled:
            logger.warning("RabbitMQBus falling back to in-memory transport because aio-pika is unavailable.")

    def publish(self, topic: str, message: Any) -> None:
        if not self._enabled:
            super().publish(topic, message)
            return
        self._run_async(self._publish(topic, message))

    def consume(self, topic: str) -> Any | None:
        if not self._enabled:
            return super().consume(topic)
        return self._run_async_result(self._consume(topic), default=None)

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> None:
        if not self._enabled:
            super().subscribe(topic, callback)
            return
        self._topic_callbacks[topic].append(callback)
        self._run_async(self._ensure_topic_subscription(topic))

    def send_p2p(self, message: P2PMessage) -> MessageAck:
        message.route = message.route or [message.from_agent, message.to_agent]
        message.delivery_mode = "tpp_direct"
        self._track_unacked(message)
        routing_key = self.agent_topic(message.to_agent)
        if self._enabled:
            self._run_async_result(self._publish_direct(routing_key, message), default=None)
        else:
            self.publish(routing_key, message)
        return self.ack(message.message_id, AckStatus.SENT, message.to_agent)

    def relay_p2p(self, message: P2PMessage, nearest_peer: str) -> MessageAck:
        route = list(message.route or [message.from_agent])
        if nearest_peer and nearest_peer not in route:
            route.append(nearest_peer)
        if message.to_agent not in route:
            route.append(message.to_agent)
        message.route = route
        message.delivery_mode = "p2p_relay"
        self._track_unacked(message)
        routing_key = self.agent_topic(message.to_agent)
        if self._enabled:
            self._run_async_result(self._publish_direct(routing_key, message), default=None)
        else:
            self.publish(routing_key, message)
        return self.ack(message.message_id, AckStatus.SENT, message.to_agent)

    def receive_for_agent(self, agent_id: str) -> P2PMessage | TaskEnvelope | None:
        if not self._enabled:
            return super().receive_for_agent(agent_id)
        message = self.consume(self.agent_topic(agent_id))
        if isinstance(message, P2PMessage) and message.requires_ack:
            self.ack(message.message_id, AckStatus.RECEIVED, agent_id)
        elif isinstance(message, TaskEnvelope):
            self.ack(message.task_id, AckStatus.RECEIVED, agent_id)
        return message

    def send_envelope(self, envelope: TaskEnvelope) -> MessageAck:
        envelope.hop_count += 1
        if envelope.hop_count >= envelope.max_hops:
            logger.error("MaxHops exceeded for TaskEnvelope %s (trace: %s)", envelope.task_id, envelope.trace_id)
            return self.mark_dead_letter_envelope(envelope, "Max hops exceeded")

        routing_key = self.agent_topic(envelope.target_agent) if envelope.target_agent else "orchestrator.inbox"
        self._track_unacked(envelope)
        if self._enabled:
            self._run_async_result(self._publish_direct(routing_key, envelope), default=None)
        else:
            self.publish(routing_key, envelope)
        return self.ack(envelope.task_id, AckStatus.SENT, envelope.target_agent or "orchestrator")

    def publish_agent_status(self, agent_id: str, payload: dict[str, Any]) -> None:
        topic = f"agent.{agent_id}.status"
        self._publish_topic(topic, payload)

    def publish_session_insights(self, session_id: str, payload: dict[str, Any]) -> None:
        topic = f"session.{session_id}.insights"
        self._publish_topic(topic, payload)

    def _publish_topic(self, topic: str, payload: dict[str, Any]) -> None:
        if self._enabled:
            self._run_async(self._publish_exchange("agents.topic", topic, payload))
        else:
            self.publish(topic, payload)

    async def _connect(self) -> None:
        if not self._enabled:
            return
        if self._channel is not None:
            return
        assert aio_pika is not None
        self._connection = await aio_pika.connect_robust(self.url)
        self._channel = await self._connection.channel()
        self._direct_exchange = await self._channel.declare_exchange("orchestrator.direct", aio_pika.ExchangeType.DIRECT, durable=True)
        self._topic_exchange = await self._channel.declare_exchange("agents.topic", aio_pika.ExchangeType.TOPIC, durable=True)

    async def _ensure_direct_queue(self, queue_name: str) -> None:
        await self._connect()
        if self._channel is None or self._direct_exchange is None or queue_name in self._direct_queues_ready:
            return
        queue = await self._channel.declare_queue(queue_name, durable=True)
        await queue.bind(self._direct_exchange, routing_key=queue_name)
        self._direct_queues_ready.add(queue_name)

    async def _publish(self, topic: str, message: Any) -> None:
        await self._publish_exchange("orchestrator.direct", topic, message)

    async def _publish_direct(self, routing_key: str, message: Any) -> None:
        await self._publish_exchange("orchestrator.direct", routing_key, message)

    async def _publish_exchange(self, exchange_name: str, routing_key: str, message: Any) -> None:
        await self._connect()
        if self._channel is None or aio_pika is None:
            return
        if exchange_name == "orchestrator.direct":
            await self._ensure_direct_queue(routing_key)
        exchange = self._direct_exchange if exchange_name == "orchestrator.direct" else self._topic_exchange
        body = json.dumps(self._serialize_message(message), ensure_ascii=True, default=str).encode("utf-8")
        await exchange.publish(aio_pika.Message(body=body), routing_key=routing_key)

    async def _consume(self, topic: str) -> Any | None:
        await self._ensure_direct_queue(topic)
        if self._channel is None:
            return None
        queue = await self._channel.declare_queue(topic, durable=True)
        incoming = await queue.get(fail=False)
        if incoming is None:
            return None
        async with incoming.process(ignore_processed=True):
            payload = json.loads(incoming.body.decode("utf-8"))
            message = self._deserialize_message(payload)
            self._track_unacked(message)
            return message

    async def _ensure_topic_subscription(self, topic: str) -> None:
        await self._connect()
        if self._channel is None or self._topic_exchange is None:
            return
        queue_name = f"sub.{topic}"
        if queue_name not in self._topic_queues_ready:
            queue = await self._channel.declare_queue(queue_name, durable=True)
            await queue.bind(self._topic_exchange, routing_key=topic)
            self._topic_queues_ready.add(queue_name)
        else:
            queue = await self._channel.declare_queue(queue_name, durable=True)

        async def _consume_forever() -> None:
            async with queue.iterator() as iterator:
                async for incoming in iterator:
                    async with incoming.process(ignore_processed=True):
                        payload = json.loads(incoming.body.decode("utf-8"))
                        data = self._deserialize_message(payload)
                        for callback in self._topic_callbacks.get(topic, []):
                            callback(data)

        self._consumer_tasks.append(asyncio.create_task(_consume_forever()))

    @staticmethod
    def _serialize_message(message: Any) -> dict[str, Any]:
        if isinstance(message, (P2PMessage, TaskEnvelope)):
            if hasattr(message, "as_dict"):
                return {"kind": "p2p" if isinstance(message, P2PMessage) else "envelope", "payload": message.as_dict()}
            return {"kind": "p2p" if isinstance(message, P2PMessage) else "envelope", "payload": asdict(message)}
        return {"kind": "raw", "payload": message}

    @staticmethod
    def _deserialize_message(payload: dict[str, Any]) -> Any:
        kind = str(payload.get("kind") or "raw").strip().lower()
        body = payload.get("payload")
        if kind == "envelope" and isinstance(body, dict):
            return TaskEnvelope(**body)
        if kind == "p2p" and isinstance(body, dict):
            return P2PMessage(**body)
        return body

    def _ensure_loop_thread(self) -> None:
        if self._loop is not None and self._loop_thread is not None and self._loop_thread.is_alive():
            return

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._loop_ready.set()
            loop.run_forever()

        self._loop_ready.clear()
        self._loop_thread = threading.Thread(target=_runner, name="rabbitmq-bus-loop", daemon=True)
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5.0)
        if self._loop is None:
            raise RuntimeError("RabbitMQ background event loop failed to start")

    def _run_async(self, coro: Any) -> None:
        if not self._enabled:
            return
        self._ensure_loop_thread()
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _run_async_result(self, coro: Any, *, default: Any) -> Any:
        if not self._enabled:
            return default
        self._ensure_loop_thread()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=30.0)
        except concurrent.futures.TimeoutError:
            future.cancel()
            logger.error("RabbitMQ async operation timed out after 30s")
            return default
