from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from datetime import datetime, UTC
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, Dict, List

from .models import AckStatus, MessageAck, P2PMessage, TaskEnvelope, TaskStatus, AgentStatus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PodState:
    agent_id: str
    status: AgentStatus
    current_task: str | None
    memory_fingerprint: str
    last_heartbeat: datetime
    capabilities: List[str]


class MessageBus:
    """
    Transparent Peer-to-Peer (TPP) Message Bus.
    Acts as a distributed mesh where Agents (Pods) communicate directly.
    """

    def __init__(self) -> None:
        self._queues: Dict[str, deque[Any]] = defaultdict(deque)
        self._subscribers: Dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._acks: Dict[str, list[MessageAck]] = defaultdict(list)
        self._unacked: Dict[str, Any] = {}
        self.dead_letters: list[Any] = []

        # TPP Pod State Management
        self.pods: Dict[str, PodState] = {}
        self._pod_inboxes: Dict[str, asyncio.Queue] = {}

    def register_pod(self, agent_id: str, capabilities: List[str]) -> None:
        self.pods[agent_id] = PodState(
            agent_id=agent_id,
            status=AgentStatus.READY,
            current_task=None,
            memory_fingerprint="",
            last_heartbeat=datetime.now(UTC),
            capabilities=capabilities,
        )
        if agent_id not in self._pod_inboxes:
            self._pod_inboxes[agent_id] = asyncio.Queue()
        logger.info("[TPP] Pod registered: %s", agent_id)

    def update_pod_state(self, agent_id: str, status: AgentStatus, task: str | None = None, fingerprint: str = "") -> None:
        if pod := self.pods.get(agent_id):
            pod.status = status
            pod.current_task = task
            pod.memory_fingerprint = fingerprint
            pod.last_heartbeat = datetime.now(UTC)
            self._gossip_state(agent_id)

    def discover_peers(self, capability: str) -> List[str]:
        return [
            pod_id
            for pod_id, state in self.pods.items()
            if capability in state.capabilities and state.status in {AgentStatus.READY, AgentStatus.IDLE}
        ]

    def _gossip_state(self, sender_id: str) -> None:
        pass

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> None:
        self._subscribers[topic].append(callback)

    def publish(self, topic: str, message: Any) -> None:
        self._queues[topic].append(message)
        for callback in self._subscribers[topic]:
            callback(message)

    def _track_unacked(self, message: Any) -> None:
        msg_id = getattr(message, "message_id", getattr(message, "task_id", None))
        if msg_id:
            self._unacked[str(msg_id)] = message

    def _deliver_to_agent(self, agent_id: str, message: Any, *, prefer_direct_inbox: bool) -> None:
        if prefer_direct_inbox and agent_id in self._pod_inboxes:
            try:
                self._pod_inboxes[agent_id].put_nowait(message)
                return
            except Exception:
                pass
        self.publish(self.agent_topic(agent_id), message)

    def consume(self, topic: str) -> Any | None:
        if not self._queues[topic]:
            return None
        msg = self._queues[topic].popleft()
        self._track_unacked(msg)
        return msg

    def send_p2p(self, message: P2PMessage) -> MessageAck:
        message.route = message.route or [message.from_agent, message.to_agent]
        message.delivery_mode = "tpp_direct"
        self._track_unacked(message)
        self._deliver_to_agent(message.to_agent, message, prefer_direct_inbox=True)
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
        self._deliver_to_agent(message.to_agent, message, prefer_direct_inbox=True)
        return self.ack(message.message_id, AckStatus.SENT, message.to_agent)

    def receive_for_agent(self, agent_id: str) -> P2PMessage | TaskEnvelope | None:
        if agent_id in self._pod_inboxes:
            try:
                msg = self._pod_inboxes[agent_id].get_nowait()
                if msg is not None:
                    if isinstance(msg, P2PMessage) and msg.requires_ack:
                        self.ack(msg.message_id, AckStatus.RECEIVED, agent_id)
                    return msg
            except Exception:
                pass

        message = self.consume(self.agent_topic(agent_id))
        if isinstance(message, P2PMessage) and message.requires_ack:
            self.ack(message.message_id, AckStatus.RECEIVED, agent_id)
        return message

    def ack(self, message_id: str, status: AckStatus | TaskStatus | str, received_by: str, reason: str | None = None) -> MessageAck:
        ack_status = self._normalize_ack_status(status)
        ack = MessageAck(message_id=message_id, ack_status=ack_status, received_by=received_by, reason=reason)
        self._acks[message_id].append(ack)

        if ack_status in {AckStatus.ACCEPTED, AckStatus.FAILED}:
            self._unacked.pop(message_id, None)

        return ack

    @staticmethod
    def _normalize_ack_status(status: AckStatus | TaskStatus | str) -> AckStatus:
        if isinstance(status, AckStatus):
            return status
        raw = status.value if hasattr(status, "value") else str(status)
        if raw in {TaskStatus.DONE.value, TaskStatus.ACCEPTED.value, "delivered"}:
            return AckStatus.ACCEPTED
        if raw in {TaskStatus.FAILED.value, TaskStatus.REJECTED.value}:
            return AckStatus.FAILED
        return AckStatus(raw)

    def ack_history(self, message_id: str) -> list[MessageAck]:
        return list(self._acks[message_id])

    def latest_ack(self, message_id: str) -> MessageAck | None:
        history = self._acks.get(message_id, [])
        return history[-1] if history else None

    def depth(self, topic: str) -> int:
        return len(self._queues[topic])

    def replay_unacked(self) -> int:
        replayed = 0
        for message in list(self._unacked.values()):
            if isinstance(message, TaskEnvelope):
                topic_agent = str(message.target_agent or "")
                if topic_agent:
                    self._deliver_to_agent(topic_agent, message, prefer_direct_inbox=False)
                    replayed += 1
            elif isinstance(message, P2PMessage):
                self._deliver_to_agent(message.to_agent, message, prefer_direct_inbox=False)
                replayed += 1
        return replayed

    def mark_dead_letter(self, message: P2PMessage, reason: str) -> MessageAck:
        self.dead_letters.append(message)
        message.is_dead_letter = True
        return self.ack(message.message_id, AckStatus.FAILED, message.to_agent, reason)

    def send_envelope(self, envelope: TaskEnvelope) -> MessageAck:
        envelope.hop_count += 1
        if envelope.hop_count >= envelope.max_hops:
            logger.error("MaxHops exceeded for TaskEnvelope %s (trace: %s)", envelope.task_id, envelope.trace_id)
            return self.mark_dead_letter_envelope(envelope, "Max hops exceeded")

        topic = self.agent_topic(envelope.target_agent) if envelope.target_agent else "orchestrator.inbox"
        logger.info("Routing TaskEnvelope %s to %s (Hop: %s/%s)", envelope.task_id, topic, envelope.hop_count, envelope.max_hops)
        self._track_unacked(envelope)
        self.publish(topic, envelope)
        return self.ack(envelope.task_id, AckStatus.SENT, envelope.target_agent or "orchestrator")

    def mark_dead_letter_envelope(self, envelope: TaskEnvelope, reason: str) -> MessageAck:
        logger.warning("Dead-lettering TaskEnvelope %s (trace: %s): %s", envelope.task_id, envelope.trace_id, reason)
        self.dead_letters.append(envelope)
        envelope.is_dead_letter = True
        return self.ack(envelope.task_id, AckStatus.FAILED, "dead_letter_queue", reason)

    @staticmethod
    def agent_topic(agent_id: str) -> str:
        return f"agent.{agent_id}.inbox"
