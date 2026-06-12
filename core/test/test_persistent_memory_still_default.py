from core.core.persistent_memory import PersistentMemoryManager


def test_persistent_memory_manager_init():
    m = PersistentMemoryManager()
    assert m is not None



def test_consolidate_episodic_uses_trained_memory_layer(monkeypatch):
    manager = PersistentMemoryManager()
    stored = []

    monkeypatch.setattr(manager, "upsert_session", lambda session_id, agent_id: f"norm-{session_id}")
    monkeypatch.setattr(
        manager,
        "retrieve_memories",
        lambda **kwargs: [
            type("R", (), {"memory_id": 11, "content": {"step": "a"}})(),
            type("R", (), {"memory_id": 12, "content": {"step": "b"}})(),
        ],
    )
    monkeypatch.setattr(
        manager,
        "store_trained_memory",
        lambda **kwargs: stored.append(kwargs) or 99,
    )

    summary = manager.consolidate_episodic(session_id="s1", agent_id="a1", chunk_size=2)

    assert summary is not None
    assert '"memory_count": 2' in summary
    assert stored and stored[0]["memory_domain"] == "episodic_summary"
    assert stored[0]["source_memory_ids"] == [11, 12]
    assert stored[0]["metadata"]["source"] == "consolidate_episodic"
