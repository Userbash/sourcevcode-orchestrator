from __future__ import annotations

from core.core.hybrid_memory import HybridMemory
from core.core.memory_settings import MemorySettings


def test_hybrid_memory_eviction_and_restore_from_persistent():
    settings = MemorySettings(hot_cache_max_entries=3, retrieval_top_k=10)
    memory = HybridMemory(settings=settings)

    memory.set("session", "s1", "k1", {"v": 1}, importance_score=0.1)
    memory.set("session", "s1", "k2", {"v": 2}, importance_score=0.9)
    memory.set("session", "s1", "k3", {"v": 3}, importance_score=0.8)
    memory.set("session", "s1", "k4", {"v": 4}, importance_score=0.7)

    evicted = memory.run_maintenance_once()
    assert evicted >= 1

    restored = memory.get("session", "s1", "k1")
    assert restored is not None
    assert "v" in str(restored)


def test_hybrid_memory_command_window_roundtrip():
    memory = HybridMemory()
    memory.remember_command(
        session_id="sess-1",
        agent_id="agent-1",
        command="task:code",
        result={"summary": "ok", "status": "done"},
        success=True,
        tokens_used=111,
    )
    window = memory.load_command_window(session_id="sess-1", agent_id="agent-1", limit=5)
    assert len(window) == 1
    assert window[0]["command"] == "task:code"


def test_hybrid_memory_key_taxonomy_for_thoughts_and_cleanup():
    memory = HybridMemory()
    memory.append_agent_thought(session_id="s-1", agent_id="a-1", thought="step-1")
    memory.append_agent_error(session_id="s-1", agent_id="a-1", error="boom")

    thought_key = memory.session_agent_thoughts_key("s-1", "a-1")
    error_key = memory.session_agent_errors_key("s-1", "a-1")

    assert memory.get_by_full_key(thought_key) == ["step-1"]
    assert memory.get_by_full_key(error_key) == ["boom"]

    removed = memory.clear_session_thoughts(session_id="s-1")
    assert removed == 2
    assert memory.get_by_full_key(thought_key) is None


def test_hybrid_memory_fast_retrieve_weighted_scoring():
    memory = HybridMemory()
    memory.set_by_full_key("session:s2:state", {"objective": "Implement login API"}, importance_score=0.6)
    memory.set_by_full_key("memory:domain:hebrew-web:patterns", "Use JWT middleware for login", importance_score=0.9)
    memory.set_by_full_key("task:t1:artifacts:diff", "Refactor CSS", importance_score=0.2)

    hits = memory.fast_retrieve(query_text="login jwt api", project_name="hebrew-web", top_k=2)
    assert len(hits) >= 1
    assert hits[0].score >= 0.0
    assert "memory:domain:hebrew-web:patterns" in hits[0].key

    brief = memory.build_context_brief(hits=hits, token_limit=50)
    assert "memory:domain:hebrew-web:patterns" in brief


def test_hybrid_memory_restore_by_key_ignores_top_k_window():
    settings = MemorySettings(hot_cache_max_entries=2, retrieval_top_k=1)
    memory = HybridMemory(settings=settings)

    memory.set("session", "s-index", "target", {"v": "old"}, importance_score=0.1)
    memory.set("session", "s-index", "newer-1", {"v": 1}, importance_score=0.9)
    memory.set("session", "s-index", "newer-2", {"v": 2}, importance_score=0.9)

    memory.run_maintenance_once()
    memory.delete("session", "s-index", "target")

    restored = memory.get("session", "s-index", "target")
    assert restored is not None
    assert "old" in str(restored)



def test_hybrid_memory_trained_memory_brief_and_context():
    memory = HybridMemory()

    class _TrainedRecord:
        def __init__(self):
            self.content = {"pattern": "split into phases"}
            self.source_memory_ids = [10, 11]
            self.quality_score = 0.9
            self.memory_domain = "prompt:code"

    memory.persistent.retrieve_trained_memories = lambda **kwargs: [_TrainedRecord()]

    brief = memory.retrieve_trained_memory_brief(session_id="s1", agent_id="a1", memory_domain="prompt:code")
    ctx = memory.get_trained_memory_context(session_id="s1", agent_id="a1", memory_domain="prompt:code")
    reused = memory.use_trained_memory(session_id="s1", agent_id="a1", memory_domain="prompt:code")

    assert "TRAINED MEMORY BRIEF" in brief
    assert "split into phases" in brief
    assert ctx["has_trained_memory"] is True
    assert ctx["brief"] == brief
    assert reused == brief



def test_hybrid_memory_ranks_trained_memory_by_quality_and_recency():
    memory = HybridMemory()

    class _Record:
        def __init__(self, quality_score, created_at, label):
            self.content = {"label": label}
            self.source_memory_ids = [1]
            self.quality_score = quality_score
            self.memory_domain = "prompt:review"
            self.created_at = created_at
            self.updated_at = created_at

    recent_low = _Record(0.2, "2025-06-12T10:00:00+00:00", "recent-low")
    old_high = _Record(0.95, "2025-05-01T10:00:00+00:00", "old-high")
    memory.persistent.retrieve_trained_memories = lambda **kwargs: [recent_low, old_high]

    brief = memory.retrieve_trained_memory_brief(session_id="s1", agent_id="a1", memory_domain="prompt:review", top_k=1)

    assert "old-high" in brief or "recent-low" in brief
    assert memory.retrieve_trained_memory_brief(session_id="s1", agent_id="a1", memory_domain="prompt:review", top_k=1) == brief


def test_hybrid_memory_trained_brief_cache_is_reused(monkeypatch):
    memory = HybridMemory()
    calls = []

    class _Record:
        def __init__(self):
            self.content = {"label": "cached"}
            self.source_memory_ids = [1]
            self.quality_score = 0.9
            self.memory_domain = "prompt:code"
            self.created_at = "2025-06-12T10:00:00+00:00"
            self.updated_at = self.created_at

    def _retrieve(**kwargs):
        calls.append(kwargs)
        return [_Record()]

    memory.persistent.retrieve_trained_memories = _retrieve
    first = memory.retrieve_trained_memory_brief(session_id="s1", agent_id="a1", memory_domain="prompt:code", top_k=1)
    second = memory.retrieve_trained_memory_brief(session_id="s1", agent_id="a1", memory_domain="prompt:code", top_k=1)

    assert first == second
    assert len(calls) == 1



def test_hybrid_memory_degrade_release_and_recovery_after_ttl():
    memory = HybridMemory()
    memory._trained_memory_degrade_ttl_sec = 1

    class _Record:
        def __init__(self):
            self.content = {"pattern": "prefer short plans"}
            self.source_memory_ids = [10]
            self.quality_score = 0.95
            self.memory_domain = "prompt:plan"
            self.created_at = "2025-06-12T10:00:00+00:00"
            self.updated_at = self.created_at

    memory.persistent.retrieve_trained_memories = lambda **kwargs: [_Record()]
    session_id = "s-degrade"
    task_type = "plan"

    memory.record_trained_memory_rejection(session_id=session_id, task_type=task_type, threshold=0.8, reason="quality_threshold")
    blocked = memory.retrieve_trained_memory_brief(session_id=session_id, agent_id="a1", memory_domain="prompt:plan", task_type=task_type)
    assert blocked == ""
    assert memory._trained_memory_degraded(session_id=session_id, task_type=task_type) is True

    memory._trained_memory_degrade[(session_id, task_type)] = __import__("datetime").datetime.now(__import__("datetime").UTC) - __import__("datetime").timedelta(seconds=1)
    released = memory.release_expired_trained_memory_degrade()
    assert released >= 1
    assert memory._trained_memory_degraded(session_id=session_id, task_type=task_type) is False
    recovered = memory.retrieve_trained_memory_brief(session_id=session_id, agent_id="a1", memory_domain="prompt:plan", task_type=task_type)
    assert "prefer short plans" in recovered
    assert memory._trained_memory_degrade_releases >= 1
