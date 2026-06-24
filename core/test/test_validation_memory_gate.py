from __future__ import annotations

from types import SimpleNamespace

from core.core.hybrid_memory import HybridMemory
from core.core.models import Task, TaskContext, TaskInput, TaskType
from core.core.validation_memory_gate import ValidationMemoryGate


class _SessionRecord:
    def __init__(self) -> None:
        self.memory_id = 7
        self.session_id = "sess-1"
        self.agent_id = "agent-1"
        self.memory_type = "episodic"
        self.content = {"summary": "Use JWT middleware for login"}
        self.metadata = {"key": "login_jwt", "scope": "session", "tags": ["auth", "jwt"]}
        self.importance_score = 0.9


class _TrainedRecord:
    def __init__(self) -> None:
        self.trained_memory_id = 9
        self.session_id = "sess-1"
        self.agent_id = "agent-1"
        self.memory_domain = "prompt:code"
        self.content = {"summary": "Prefer phased changes and regression tests"}
        self.metadata = {"source": "test"}
        self.quality_score = 0.95


class _FakeLayered:
    def build_context_pie(self, task, agent_id: str, provider: str = "", model_name: str = "", token_limit: int = 240):
        return SimpleNamespace(
            layered_context_brief="layered context brief",
            prompt_guidance=["repeat acceptance criteria verbatim"],
            prompt_memory_brief="prompt memory brief",
            routing_memory_brief="routing memory brief",
            execution_memory_brief="execution memory brief",
        )


class _FakeVFS:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, object], str, dict[str, object]]] = []

    def write_state(self, path: str, content: dict[str, object], agent_id: str, metadata: dict[str, object] | None = None) -> bool:
        self.writes.append((path, content, agent_id, metadata or {}))
        return True

    def read_state(self, path: str):
        if not self.writes:
            return None
        last_path, content, agent_id, metadata = self.writes[-1]
        return SimpleNamespace(path=last_path, content=content, checksum="ok", last_updated="now", owner_agent=agent_id, integrity="valid", metadata=metadata)

    def finalize(self):
        return {"node_count": len(self.writes), "storage": "filesystem", "integrity": "healthy"}


class _FakeSessionMemory:
    def __init__(self, hybrid: HybridMemory) -> None:
        self.hybrid = hybrid
        self.layered = _FakeLayered()


class _FakeAPI:
    def __init__(self, session_memory: _FakeSessionMemory, unified_vfs: _FakeVFS) -> None:
        self._contexts = {"session_memory": session_memory, "unified_vfs": unified_vfs}
        self.logs: list[tuple[str, str]] = []

    def get_context(self, key: str):
        return self._contexts.get(key)

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


def _task() -> Task:
    return Task(
        TaskType.CODE,
        TaskInput(
            "Implement a safer memory warmup path",
            files=["core/core/hybrid_memory.py"],
            constraints=["preserve existing hot cache behavior"],
            acceptance_criteria=["DB snapshot stored", "VFS snapshot stored"],
        ),
        TaskContext("demo", "/repo/demo", "main"),
        session_id="sess-1",
    )


def test_hybrid_memory_warmup_populates_hot_cache_and_search_indices():
    memory = HybridMemory()
    memory.persistent.list_session_memories = lambda **kwargs: [_SessionRecord()]
    memory.persistent.retrieve_trained_memories = lambda **kwargs: [_TrainedRecord()]

    warmup = memory.warmup_from_persistent(session_id="sess-1", agent_id="agent-1", memory_domain="prompt:code", top_k=3, trained_top_k=2)
    hits = memory.fast_retrieve(query_text="jwt login", session_id="sess-1", top_k=1)

    assert warmup["warmup_total"] == 2
    assert warmup["warmed_session_records"] == 1
    assert warmup["warmed_trained_records"] == 1
    assert warmup["warmed_keys"]
    assert hits
    assert "login_jwt" in hits[0].key or "trained:prompt:code" in hits[0].key


def test_validation_memory_gate_builds_and_persists_consensus_snapshot():
    memory = HybridMemory()
    memory.persistent.list_session_memories = lambda **kwargs: [_SessionRecord()]
    memory.persistent.retrieve_trained_memories = lambda **kwargs: [_TrainedRecord()]
    memory.persistent.store_memory = lambda **kwargs: 101
    session_memory = _FakeSessionMemory(memory)
    vfs = _FakeVFS()
    api = _FakeAPI(session_memory, vfs)

    gate = ValidationMemoryGate()
    gate.on_load(api)
    snapshot = gate.build_validation_context(_task(), agent_id="codex-main", provider="antigravity", model_name="gemini-3.5-flash")

    assert snapshot["trained_memory_domain"] == "prompt:code"
    assert snapshot["validation_snapshot_stored"] is True
    assert snapshot["validation_vfs_stored"] is True
    assert snapshot["validation_memory_consensus"] > 0
    assert snapshot["warmup"]["warmed_session_records"] == 1
    assert snapshot["warmup"]["warmed_trained_records"] == 1
    assert vfs.writes
    assert vfs.writes[-1][0].startswith("validation/memory/")
    assert any(entry[0] == "info" for entry in api.logs)
