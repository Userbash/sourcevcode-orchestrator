from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .message_bus import MessageBus
from .models import AckStatus, MessageAck, TaskEnvelope

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeliveryRecord:
    envelope: TaskEnvelope
    status: str
    sent_at: datetime
    last_progress_at: datetime
    received_at: datetime | None = None
    completed_at: datetime | None = None
    received_by: str | None = None
    last_reason: str | None = None
    retry_count: int = 0
    last_audited_status: str | None = None
    handshake_state: str = "syn"
    payload_checksum: str = ""
    payload_validated: bool = False


class DeliverySupervisor:
    DELIVERY_EVENTS_TOPIC = "delivery.events"

    def __init__(
        self,
        message_bus: MessageBus,
        *,
        session_memory: Any | None = None,
        kpi_events: Any | None = None,
        ack_timeout_sec: int = 30,
        now_fn: Any | None = None,
    ) -> None:
        self.message_bus = message_bus
        self.session_memory = session_memory
        self.kpi_events = kpi_events
        self.ack_timeout_sec = max(1, int(ack_timeout_sec))
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._records: dict[str, DeliveryRecord] = {}

    def dispatch(self, envelope: TaskEnvelope) -> MessageAck:
        ack = self.message_bus.send_envelope(envelope)
        now = self._now()
        record = self._records.get(envelope.task_id)
        if record is None:
            record = DeliveryRecord(
                envelope=envelope,
                status=ack.ack_status.value,
                sent_at=now,
                last_progress_at=now,
                retry_count=int(envelope.retry_count),
                handshake_state="syn",
                payload_checksum=self._payload_checksum(envelope),
            )
            self._records[envelope.task_id] = record
        else:
            record.envelope = envelope
            record.status = ack.ack_status.value
            record.last_progress_at = now
            record.retry_count = int(envelope.retry_count)
            record.handshake_state = "syn"
            record.payload_checksum = self._payload_checksum(envelope)
            record.payload_validated = False
            record.last_reason = None
        self._audit("delivery.sent", record)
        self.refresh(envelope.task_id)
        return ack

    def refresh(self, task_id: str) -> dict[str, Any]:
        record = self._records[task_id]
        history = self.message_bus.ack_history(task_id)
        self._apply_history(record, history)
        snapshot = self._snapshot(record, history)
        self._persist_snapshot(snapshot)
        return snapshot

    def snapshot(self, task_id: str) -> dict[str, Any]:
        record = self._records[task_id]
        history = self.message_bus.ack_history(task_id)
        return self._snapshot(record, history)

    def fetch_agent_mailbox(self, agent_id: str, *, limit: int = 1) -> list[TaskEnvelope]:
        pulled: list[TaskEnvelope] = []
        for _ in range(max(1, int(limit))):
            message = self.message_bus.receive_for_agent(agent_id)
            if not isinstance(message, TaskEnvelope):
                break
            pulled.append(message)
            record = self._records.get(message.task_id)
            if record is not None:
                record.received_by = agent_id
                record.received_at = record.received_at or self._now()
                record.last_progress_at = self._now()
                record.handshake_state = "syn_ack"
            self._persist_snapshot(self.snapshot(message.task_id))
        return pulled

    def confirm_payload(self, task_id: str, agent_id: str, envelope: TaskEnvelope) -> bool:
        record = self._records[task_id]
        actual_checksum = self._payload_checksum(envelope)
        expected_checksum = record.payload_checksum
        record.received_by = agent_id
        record.received_at = record.received_at or self._now()
        record.last_progress_at = self._now()
        if actual_checksum != expected_checksum:
            record.payload_validated = False
            record.handshake_state = "invalid"
            record.last_reason = "payload_checksum_mismatch"
            self._audit("delivery.invalid_payload", record, extra={"reason": record.last_reason})
            self._persist_snapshot(self.snapshot(task_id))
            return False
        record.payload_validated = True
        record.handshake_state = "ack_valid"
        self._audit("delivery.payload_validated", record)
        self._persist_snapshot(self.snapshot(task_id))
        return True

    def establish_delivery(self, task_id: str, agent_id: str) -> MessageAck:
        record = self._records[task_id]
        if not record.payload_validated:
            ack = self.message_bus.ack(task_id, status=AckStatus.FAILED, received_by=agent_id, reason="payload_not_validated")
            record.status = AckStatus.FAILED.value
            record.last_reason = "payload_not_validated"
            record.handshake_state = "invalid"
            self.refresh(task_id)
            return ack
        ack = self.message_bus.ack(task_id, status=AckStatus.RECEIVED, received_by=agent_id)
        record.handshake_state = "established"
        record.last_progress_at = self._now()
        self._audit("delivery.established", record)
        self.refresh(task_id)
        return ack

    def mailbox_snapshot(self, agent_id: str) -> dict[str, Any]:
        topic = self.message_bus.agent_topic(agent_id)
        tracked = [task_id for task_id, record in self._records.items() if str(record.envelope.target_agent or "") == agent_id and record.status not in {AckStatus.ACCEPTED.value, AckStatus.FAILED.value}]
        return {
            "agent_id": agent_id,
            "queue_depth": self.message_bus.depth(topic),
            "tracked_task_ids": tracked,
        }

    def inspect_timeouts(self) -> dict[str, int]:
        retried = 0
        dead_lettered = 0
        now = self._now()
        for task_id, record in list(self._records.items()):
            history = self.message_bus.ack_history(task_id)
            self._apply_history(record, history)
            if record.status in {AckStatus.ACCEPTED.value, AckStatus.FAILED.value}:
                continue
            age_sec = (now - record.last_progress_at).total_seconds()
            if age_sec < self.ack_timeout_sec:
                continue
            if int(record.envelope.retry_count) < int(record.envelope.max_retries):
                record.envelope.retry_count += 1
                record.retry_count = int(record.envelope.retry_count)
                record.last_progress_at = now
                self.message_bus.send_envelope(record.envelope)
                retried += 1
                self._audit("delivery.retry", record, extra={"reason": "ack_timeout"})
            else:
                self.message_bus.mark_dead_letter_envelope(record.envelope, "ack_timeout")
                record.status = AckStatus.FAILED.value
                record.last_reason = "ack_timeout"
                record.completed_at = now
                record.last_progress_at = now
                dead_lettered += 1
                self._audit("delivery.dead_letter", record, extra={"reason": "ack_timeout"})
            self._persist_snapshot(self._snapshot(record, self.message_bus.ack_history(task_id)))
        return {"retried": retried, "dead_lettered": dead_lettered}

    def delivery_health_snapshot(self) -> dict[str, Any]:
        pending = 0
        failed = 0
        accepted = 0
        max_lag_sec = 0.0
        now = self._now()
        by_agent: dict[str, dict[str, Any]] = {}
        for task_id, record in self._records.items():
            history = self.message_bus.ack_history(task_id)
            self._apply_history(record, history)
            lag_sec = max(0.0, (now - record.last_progress_at).total_seconds())
            max_lag_sec = max(max_lag_sec, lag_sec)
            agent = str(record.envelope.target_agent or "orchestrator")
            topic = self.message_bus.agent_topic(agent) if record.envelope.target_agent else "orchestrator.inbox"
            item = by_agent.setdefault(agent, {"queue_depth": self.message_bus.depth(topic), "pending": 0, "accepted": 0, "failed": 0})
            if record.status == AckStatus.ACCEPTED.value:
                accepted += 1
                item["accepted"] += 1
            elif record.status == AckStatus.FAILED.value:
                failed += 1
                item["failed"] += 1
            else:
                pending += 1
                item["pending"] += 1
        return {
            "tracked": len(self._records),
            "pending": pending,
            "accepted": accepted,
            "failed": failed,
            "max_lag_sec": round(max_lag_sec, 3),
            "by_agent": by_agent,
        }

    def _apply_history(self, record: DeliveryRecord, history: list[Any]) -> None:
        if not history:
            return
        for ack in history:
            status = getattr(getattr(ack, "ack_status", None), "value", str(getattr(ack, "ack_status", "")))
            received_by = getattr(ack, "received_by", None)
            reason = getattr(ack, "reason", None)
            if status == AckStatus.RECEIVED.value:
                record.status = status
                record.received_at = record.received_at or self._now()
                record.received_by = received_by
                record.last_progress_at = self._now()
                if record.handshake_state == "syn":
                    record.handshake_state = "syn_ack"
            elif status == AckStatus.ACCEPTED.value:
                record.status = status
                record.received_by = received_by
                record.completed_at = self._now()
                record.last_progress_at = self._now()
                record.handshake_state = "established" if record.payload_validated else record.handshake_state
                if record.last_audited_status != status:
                    self._audit("delivery.accepted", record)
                    record.last_audited_status = status
            elif status == AckStatus.FAILED.value:
                record.status = status
                record.received_by = received_by
                record.last_reason = reason
                record.completed_at = self._now()
                record.last_progress_at = self._now()
                if record.last_audited_status != status:
                    self._audit("delivery.failed", record, extra={"reason": reason})
                    record.last_audited_status = status
            elif status == AckStatus.SENT.value:
                record.status = status

    def _snapshot(self, record: DeliveryRecord, history: list[Any]) -> dict[str, Any]:
        return {
            "task_id": record.envelope.task_id,
            "target_agent": record.envelope.target_agent,
            "status": record.status,
            "retry_count": int(record.retry_count),
            "max_retries": int(record.envelope.max_retries),
            "received_by": record.received_by,
            "last_reason": record.last_reason,
            "sent_at": record.sent_at.isoformat(),
            "received_at": record.received_at.isoformat() if record.received_at else None,
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            "ack_history": [getattr(getattr(item, "ack_status", None), "value", str(getattr(item, "ack_status", ""))) for item in history],
            "queue_depth": self.message_bus.depth(self.message_bus.agent_topic(record.envelope.target_agent)) if record.envelope.target_agent else self.message_bus.depth("orchestrator.inbox"),
            "handshake_state": record.handshake_state,
            "payload_validated": record.payload_validated,
            "payload_checksum": record.payload_checksum,
        }

    def _payload_checksum(self, envelope: TaskEnvelope) -> str:
        payload = envelope.payload.as_dict() if hasattr(envelope.payload, "as_dict") else envelope.payload
        return json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)

    def _persist_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.session_memory is None:
            return
        try:
            self.session_memory.set("task", str(snapshot["task_id"]), "delivery_status", snapshot, ttl_sec=86400)
        except Exception as exc:
            logger.warning("[DELIVERY] memory audit write failed: %s", exc)

    def _audit(self, event_type: str, record: DeliveryRecord, *, extra: dict[str, Any] | None = None) -> None:
        payload = {
            "event_type": event_type,
            "topic": self.DELIVERY_EVENTS_TOPIC,
            "task_id": record.envelope.task_id,
            "target_agent": record.envelope.target_agent,
            "status": record.status,
            "retry_count": int(record.retry_count),
            "max_retries": int(record.envelope.max_retries),
            "recorded_at": self._now().isoformat(),
        }
        if extra:
            payload.update(extra)
        try:
            self.message_bus.publish(self.DELIVERY_EVENTS_TOPIC, dict(payload))
        except Exception as exc:
            logger.warning("[DELIVERY] topic publish failed: %s", exc)
        if self.kpi_events is not None and hasattr(self.kpi_events, "write"):
            try:
                self.kpi_events.write(payload)
            except Exception as exc:
                logger.warning("[DELIVERY] KPI audit write failed: %s", exc)
        if self.session_memory is not None:
            try:
                self.session_memory.set("task", record.envelope.task_id, "delivery_last_event", payload, ttl_sec=86400)
            except Exception as exc:
                logger.warning("[DELIVERY] memory audit write failed: %s", exc)

    def _now(self) -> datetime:
        value = self._now_fn()
        if isinstance(value, datetime):
            return value
        return datetime.now(UTC)
