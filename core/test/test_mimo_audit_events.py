from __future__ import annotations

from core.mimo.proxy import MimoOrchestrationDirector


class DummyTaskType:
    def __init__(self, value: str) -> None:
        self.value = value


def test_director_safe_fallback_emits_formalized_mimo_unavailable_event():
    director = MimoOrchestrationDirector()
    director.is_available = False
    task = type("T", (), {
        "session_id": "sx",
        "task_id": "tx",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "medium"})(),
        "type": DummyTaskType("docs"),
        "input": type("I", (), {"description": "docs update", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "normal"})(),
    })()

    recommendation = director.recommend_model(task, {"local_llm": {"ready": True}}, current_budget=1000.0)

    assert any(
        item.get("event_type") == "MIMO_UNAVAILABLE_DECISION"
        for item in recommendation.selection_trace
    )
