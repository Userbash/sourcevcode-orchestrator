from __future__ import annotations

from core.core.agent_factory import AgentFactory
from core.core.hybrid_memory import HybridMemory
from core.core.memory_settings import MemorySettings
from core.core.persistent_memory import PersistentMemoryManager


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def publish(self, topic: str, message: dict[str, object]) -> None:
        self.events.append((topic, message))



def test_agent_factory_wires_session_memory_events_to_bus(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path / "memory_store"))
    bus = _RecordingBus()
    monkeypatch.setattr(AgentFactory, "_build_message_bus", staticmethod(lambda: bus))

    components = AgentFactory.build()
    components.session_memory.set("sess-1", "summary", {"status": "ok"})
    components.session_memory.hybrid.soft_flush()

    assert bus.events
    topic, payload = bus.events[-1]
    assert topic == "memory.events"
    assert payload["event_type"] == "memory.stored"
    assert payload["session_id"] == "sess-1"
    assert payload["memory_type"] == "episodic"



def test_persistent_memory_manager_emits_trained_memory_store_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path / "memory_store"))
    manager = PersistentMemoryManager(MemorySettings(enabled=False))
    events: list[tuple[str, dict[str, object]]] = []
    manager.set_event_publisher(lambda topic, payload: events.append((topic, payload)))

    trained_id = manager.store_trained_memory(
        session_id="sess-2",
        agent_id="trainer-1",
        memory_domain="prompt:code",
        content={"summary": "prefer small safe diffs"},
        source_memory_ids=[11, 12],
        quality_score=0.93,
        metadata={"source": "test"},
    )

    assert trained_id > 0
    assert events
    topic, payload = events[-1]
    assert topic == "memory.trained.events"
    assert payload["event_type"] == "memory.trained.stored"
    assert payload["session_id"] == "sess-2"
    assert payload["memory_domain"] == "prompt:code"
    assert payload["quality_score"] == 0.93



def test_persistent_memory_manager_emits_command_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path / "memory_store"))
    manager = PersistentMemoryManager(MemorySettings(enabled=False))
    events: list[tuple[str, dict[str, object]]] = []
    manager.set_event_publisher(lambda topic, payload: events.append((topic, payload)))

    manager.store_command(
        session_id="sess-5",
        agent_id="agent-2",
        command="task:review",
        result={"status": "done"},
        success=True,
        tokens_used=42,
    )

    assert events
    topic, payload = events[-1]
    assert topic == "memory.events"
    assert payload["event_type"] == "memory.command.remembered"
    assert payload["session_id"] == "sess-5"
    assert payload["command"] == "task:review"
    assert payload["tokens_used"] == 42


def test_hybrid_memory_emits_trained_memory_outcome_and_rejection_events():
    memory = HybridMemory()
    events: list[tuple[str, dict[str, object]]] = []
    memory.set_event_publisher(lambda topic, payload: events.append((topic, payload)))

    memory.record_trained_memory_outcome(
        session_id="sess-3",
        task_type="review",
        accepted=False,
        threshold=0.8,
        reason="quality_threshold",
    )
    memory.record_trained_memory_rejection(
        session_id="sess-3",
        task_type="review",
        threshold=0.8,
        reason="quality_threshold",
    )

    event_types = [payload["event_type"] for _, payload in events]
    assert "memory.trained.outcome" in event_types
    assert "memory.trained.rejected" in event_types



def test_memory_event_publish_failure_does_not_break_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path / "memory_store"))
    manager = PersistentMemoryManager(MemorySettings(enabled=False))
    manager.set_event_publisher(lambda topic, payload: (_ for _ in ()).throw(RuntimeError("broker down")))

    memory_id = manager.store_memory(
        session_id="sess-4",
        agent_id="agent-1",
        memory_type="episodic",
        content={"status": "stored"},
        metadata={"key": "health"},
    )

    assert memory_id > 0
    rows = manager.retrieve_memories(session_id="sess-4", agent_id="agent-1", memory_type="episodic")
    assert len(rows) == 1
    assert rows[0].content == {"status": "stored"}
