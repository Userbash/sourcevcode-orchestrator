from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.agents.base_agent import BaseAgent
from core.core.delivery_supervisor import DeliverySupervisor
from core.core.effectiveness_dashboard import build_kpi_dashboard
from core.core.message_bus import MessageBus
from core.core.models import Task, TaskContext, TaskInput, TaskPayload, TaskStatus, TaskType, encapsulate
from core.core.orchestrator import Orchestrator
from core.core.sourcecraft_module import SourceCraftModule
from core.core.local_llm_module import LocalLLMModule


@dataclass
class _Clock:
    now_value: datetime

    def now(self) -> datetime:
        return self.now_value

    def advance(self, seconds: int) -> None:
        self.now_value = self.now_value + timedelta(seconds=seconds)


class _MemoryRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, str, object]] = []

    def set(self, scope, identifier: str, key: str, value: object, **kwargs) -> None:
        self.calls.append((scope, identifier, key, value))


class _KPIRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def write(self, payload: dict[str, object]) -> None:
        self.events.append(payload)


class _ExplodingMemory:
    def set(self, *args, **kwargs) -> None:
        raise RuntimeError("memory sink down")


class _ExplodingKPI:
    def write(self, payload: dict[str, object]) -> None:
        raise RuntimeError("kpi sink down")


class _HandshakeAgent(BaseAgent):
    def __init__(self, agent_id: str = "mailbox-coder") -> None:
        super().__init__(agent_id, ["mailbox_code"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(task, "executed through delivery handshake", TaskStatus.DONE)



def _envelope(*, target_agent: str = "coder-1", max_retries: int = 2):
    payload = TaskPayload("Implement delivery supervision", {}, {}, ["done"], "json", [])
    return encapsulate(payload, {"target_agent": target_agent, "max_hops": 5, "max_retries": max_retries})



def _task(agent_id: str = "mailbox-coder") -> Task:
    task = Task(
        type=TaskType.CODE,
        input=TaskInput(description="implement mailbox execution"),
        context=TaskContext(project="demo", repo_path=".", branch="main"),
    )
    task.required_capability = "mailbox_code"
    task.assigned_model = "local-small"
    return task



def test_delivery_supervisor_tracks_sent_received_and_accepted():
    bus = MessageBus()
    memory = _MemoryRecorder()
    kpi = _KPIRecorder()
    clock = _Clock(datetime(2026, 6, 14, tzinfo=UTC))
    supervisor = DeliverySupervisor(bus, session_memory=memory, kpi_events=kpi, ack_timeout_sec=10, now_fn=clock.now)
    envelope = _envelope()

    sent = supervisor.dispatch(envelope)
    assert sent.ack_status.value == "sent"

    bus.receive_for_agent("coder-1")
    bus.ack(envelope.task_id, TaskStatus.DONE, "coder-1")
    snapshot = supervisor.refresh(envelope.task_id)

    assert snapshot["status"] == "accepted"
    assert snapshot["received_by"] == "coder-1"
    assert snapshot["ack_history"] == ["sent", "received", "accepted"]
    assert any(call[2] == "delivery_status" for call in memory.calls)
    assert any(event.get("event_type") == "delivery.accepted" for event in kpi.events)



def test_delivery_supervisor_publishes_handshake_audit_to_delivery_events_topic():
    bus = MessageBus()
    clock = _Clock(datetime(2026, 6, 14, tzinfo=UTC))
    supervisor = DeliverySupervisor(bus, ack_timeout_sec=10, now_fn=clock.now)
    envelope = _envelope()

    supervisor.dispatch(envelope)
    pulled = supervisor.fetch_agent_mailbox("coder-1", limit=1)
    assert supervisor.confirm_payload(envelope.task_id, "coder-1", pulled[0]) is True
    supervisor.establish_delivery(envelope.task_id, "coder-1")

    events = []
    while True:
        event = bus.consume("delivery.events")
        if event is None:
            break
        events.append(event)

    event_types = [event["event_type"] for event in events]
    assert "delivery.sent" in event_types
    assert "delivery.payload_validated" in event_types
    assert "delivery.accepted" in event_types
    assert all(event["topic"] == "delivery.events" for event in events)



def test_delivery_supervisor_retries_stuck_messages_before_dead_letter():
    bus = MessageBus()
    clock = _Clock(datetime(2026, 6, 14, tzinfo=UTC))
    supervisor = DeliverySupervisor(bus, ack_timeout_sec=5, now_fn=clock.now)
    envelope = _envelope(max_retries=2)

    supervisor.dispatch(envelope)
    clock.advance(6)
    summary = supervisor.inspect_timeouts()
    snapshot = supervisor.snapshot(envelope.task_id)

    assert summary["retried"] == 1
    assert snapshot["retry_count"] == 1
    assert snapshot["status"] == "sent"
    assert bus.depth("agent.coder-1.inbox") == 2



def test_delivery_supervisor_dead_letters_after_retry_budget_exhausted():
    bus = MessageBus()
    clock = _Clock(datetime(2026, 6, 14, tzinfo=UTC))
    supervisor = DeliverySupervisor(bus, ack_timeout_sec=5, now_fn=clock.now)
    envelope = _envelope(max_retries=0)

    supervisor.dispatch(envelope)
    clock.advance(6)
    summary = supervisor.inspect_timeouts()
    snapshot = supervisor.snapshot(envelope.task_id)

    assert summary["dead_lettered"] == 1
    assert snapshot["status"] == "failed"
    assert snapshot["last_reason"] == "ack_timeout"
    assert len(bus.dead_letters) == 1
    assert bus.dead_letters[0].task_id == envelope.task_id



def test_delivery_supervisor_audit_sink_failures_do_not_break_dispatch_or_refresh():
    bus = MessageBus()
    clock = _Clock(datetime(2026, 6, 14, tzinfo=UTC))
    supervisor = DeliverySupervisor(bus, session_memory=_ExplodingMemory(), kpi_events=_ExplodingKPI(), ack_timeout_sec=5, now_fn=clock.now)
    envelope = _envelope()

    supervisor.dispatch(envelope)
    bus.receive_for_agent("coder-1")
    bus.ack(envelope.task_id, TaskStatus.DONE, "coder-1")
    snapshot = supervisor.refresh(envelope.task_id)

    assert snapshot["status"] == "accepted"



def test_orchestrator_wires_delivery_supervisor():
    orchestrator = Orchestrator()

    assert orchestrator.delivery_supervisor.message_bus is orchestrator.message_bus
    assert orchestrator.delivery_supervisor.session_memory is orchestrator.session_memory



def test_delivery_supervisor_handshake_establishes_only_after_payload_validation():
    bus = MessageBus()
    clock = _Clock(datetime(2026, 6, 14, tzinfo=UTC))
    supervisor = DeliverySupervisor(bus, ack_timeout_sec=10, now_fn=clock.now)
    envelope = _envelope()

    supervisor.dispatch(envelope)
    pulled = supervisor.fetch_agent_mailbox("coder-1", limit=1)
    assert len(pulled) == 1

    syn_snapshot = supervisor.snapshot(envelope.task_id)
    assert syn_snapshot["handshake_state"] == "syn_ack"

    validated = supervisor.confirm_payload(envelope.task_id, "coder-1", pulled[0])
    assert validated is True

    established = supervisor.establish_delivery(envelope.task_id, "coder-1")
    assert established.ack_status.value == "accepted"

    snapshot = supervisor.refresh(envelope.task_id)
    assert snapshot["status"] == "accepted"
    assert snapshot["handshake_state"] == "established"
    assert snapshot["payload_validated"] is True



def test_delivery_supervisor_rejects_invalid_payload_and_keeps_retryable_state():
    bus = MessageBus()
    clock = _Clock(datetime(2026, 6, 14, tzinfo=UTC))
    supervisor = DeliverySupervisor(bus, ack_timeout_sec=10, now_fn=clock.now)
    envelope = _envelope()

    supervisor.dispatch(envelope)
    pulled = supervisor.fetch_agent_mailbox("coder-1", limit=1)
    assert len(pulled) == 1

    tampered = pulled[0].model_copy(deep=True)
    tampered.payload.objective = "tampered"

    validated = supervisor.confirm_payload(envelope.task_id, "coder-1", tampered)
    snapshot = supervisor.refresh(envelope.task_id)

    assert validated is False
    assert snapshot["handshake_state"] == "invalid"
    assert snapshot["payload_validated"] is False
    assert snapshot["last_reason"] == "payload_checksum_mismatch"



def test_delivery_supervisor_exposes_mailbox_buffer_depth_by_agent():
    bus = MessageBus()
    supervisor = DeliverySupervisor(bus, ack_timeout_sec=10)
    first = _envelope(target_agent="agent-a")
    second = _envelope(target_agent="agent-a")

    supervisor.dispatch(first)
    supervisor.dispatch(second)

    mailbox = supervisor.mailbox_snapshot("agent-a")

    assert mailbox["agent_id"] == "agent-a"
    assert mailbox["queue_depth"] == 2
    assert first.task_id in mailbox["tracked_task_ids"]
    assert second.task_id in mailbox["tracked_task_ids"]



def test_orchestrator_exposes_mailbox_and_handshake_api():
    orchestrator = Orchestrator()
    envelope = _envelope()
    orchestrator.dispatch_envelope(envelope)

    pulled = orchestrator.fetch_agent_mailbox("coder-1", limit=1)
    assert len(pulled) == 1
    assert orchestrator.confirm_delivery_payload(envelope.task_id, "coder-1", pulled[0]) is True

    ack = orchestrator.establish_delivery_handshake(envelope.task_id, "coder-1")
    snapshot = orchestrator.refresh_delivery(envelope.task_id)

    assert ack.ack_status.value == "accepted"
    assert snapshot["handshake_state"] == "established"
    assert orchestrator.mailbox_snapshot("coder-1")["agent_id"] == "coder-1"



def test_orchestrator_runs_local_agent_through_delivery_mailbox_handshake(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_PREFLIGHT_LIVE_PROBE", "false")
    monkeypatch.setenv("AI_BRIDGE_AUTOSTART_LOCAL_LLM", "false")
    monkeypatch.setenv("AI_BRIDGE_AUTOSTART_EASY_DIFFUSION", "false")
    monkeypatch.setattr(SourceCraftModule, "on_load", lambda self, api: None)
    monkeypatch.setattr(LocalLLMModule, "build_decomposition_draft", lambda self, task, context: {"ready": False, "should_delegate": False, "task_family": "code"})
    orchestrator = Orchestrator()
    agent = _HandshakeAgent()
    orchestrator.attach_local_agent(agent.agent_id, agent, agent_type="codex", provider="local", model_name="local-small")

    task = _task(agent.agent_id)
    result = orchestrator._run_local_agent_via_delivery(task, agent.agent_id, task.required_capability or "mailbox_code", agent, {})

    assert result.status == TaskStatus.DONE
    delivery = orchestrator.delivery_health_snapshot()
    assert delivery["accepted"] >= 1
    assert delivery["by_agent"][agent.agent_id]["accepted"] >= 1

    topic_events = []
    while True:
        event = orchestrator.message_bus.consume("delivery.events")
        if event is None:
            break
        topic_events.append(event)
    assert any(event["task_id"] == task.task_id for event in topic_events)



def test_kpi_dashboard_includes_delivery_backlog_lag_and_dead_letter_metrics(tmp_path: Path):
    kpi_log = tmp_path / "kpi_events.jsonl"
    kpi_log.write_text(
        "\n".join(
            [
                '{"event_type":"delivery.sent","task_id":"t1","logged_at":"2026-06-14T00:00:00+00:00"}',
                '{"event_type":"delivery.retry","task_id":"t1","logged_at":"2026-06-14T00:01:00+00:00"}',
                '{"event_type":"delivery.dead_letter","task_id":"t1","reason":"ack_timeout","logged_at":"2026-06-14T00:02:00+00:00"}',
                '{"event_type":"delivery.accepted","task_id":"t2","logged_at":"2026-06-14T00:03:00+00:00"}',
            ]
        ) + "\n",
        encoding="utf-8",
    )
    rolling = tmp_path / "rolling_kpi_store.json"
    rolling.write_text("{}", encoding="utf-8")

    dashboard = build_kpi_dashboard(
        kpi_log_path=kpi_log,
        rolling_kpi_path=rolling,
        delivery_snapshot={
            "tracked": 3,
            "pending": 1,
            "accepted": 1,
            "failed": 1,
            "max_lag_sec": 12.5,
            "by_agent": {"coder-1": {"queue_depth": 2, "pending": 1, "accepted": 1, "failed": 0}},
        },
    )

    assert dashboard["delivery"]["tracked"] == 3
    assert dashboard["delivery"]["backlog"] == 1
    assert dashboard["delivery"]["max_lag_sec"] == 12.5
    assert dashboard["delivery"]["dead_lettered_last_24h"] == 1
    assert dashboard["delivery"]["retried_last_24h"] == 1
    assert dashboard["delivery"]["acceptance_rate"] == 0.5
