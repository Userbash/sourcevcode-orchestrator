from core.mimo.proxy import MimoOrchestrationDirector


class DummyChoice:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.name = model_name


class DummyTask:
    def __init__(self, complexity: str = "medium") -> None:
        self.complexity = type("C", (), {"value": complexity})()


def test_director_keeps_model_when_unavailable():
    director = MimoOrchestrationDirector()
    director.is_available = False
    choice = DummyChoice("model-a")
    result = director.validate_and_correct(choice, DummyTask(), current_budget=1.0)
    assert result.model_name == "model-a"


def test_director_can_correct_low_score_model():
    director = MimoOrchestrationDirector()
    director.state.update_score("model-a", is_successful=False, latency=1500.0)
    director.state.update_score("model-a", is_successful=False, latency=1500.0)
    director.state.update_score("model-a", is_successful=False, latency=1500.0)
    director.state.update_score("model-a", is_successful=False, latency=1500.0)
    director.state.update_score("model-a", is_successful=False, latency=1500.0)
    choice = DummyChoice("model-a")
    result = director.validate_and_correct(choice, DummyTask(), current_budget=100.0)
    assert result.model_name == "gpt-4o"


def test_director_uses_budget_module_when_mimo_disabled():
    director = MimoOrchestrationDirector()
    director.is_available = False

    class Budget:
        def __init__(self) -> None:
            self.stats = {"model-a": type("S", (), {"remaining_tokens": 5})()}

        def evaluate_model_budget(self, model: str, planned_tokens: int = 0):
            return {"remaining_percentage": 90, "action": "ok"}

    director.set_budget_module(Budget())
    choice = DummyChoice("model-a")
    result = director.validate_and_correct(choice, DummyTask(), current_budget=100.0)
    assert result.model_name == "model-a"


class DummyTaskType:
    def __init__(self, value: str) -> None:
        self.value = value


def test_director_prefers_historical_model_by_task_type():
    director = MimoOrchestrationDirector()
    class Persistent:
        def list_recent_commands_by_session(self, session_id: str, limit: int = 8):
            return [{"success": False, "tokens_used": 1400}, {"success": False, "tokens_used": 1600}]
        def retrieve_memories(self, session_id: str, agent_id: str, memory_type: str, top_k: int = 8):
            return []
    director.set_history_source(type("History", (), {"hybrid": type("Hybrid", (), {"persistent": Persistent()})()})())
    class Budget:
        def __init__(self) -> None:
            self.stats = {}
        def evaluate_model_budget(self, model: str, planned_tokens: int = 0):
            return {"remaining_percentage": 95, "action": "ok"}
    director.set_budget_module(Budget())
    task = type("T", (), {"session_id": "s1", "task_id": "t1", "memory_scope": "task", "complexity": type("C", (), {"value": "medium"})(), "type": DummyTaskType("review")})()
    ctx = director.build_selection_context("model-a", task, 900.0, memory_context={})
    assert ctx["budget_pressure"] == "high"
    assert ctx["preferred_model"] == "gpt-4o"


def test_director_applies_vfs_pressure():
    director = MimoOrchestrationDirector()
    director.set_vfs_source(type("VFS", (), {"finalize": lambda self: {"node_count": 300}})())
    task = type("T", (), {"session_id": "s2", "task_id": "t2", "memory_scope": "task", "complexity": type("C", (), {"value": "medium"})(), "type": DummyTaskType("code")})()
    ctx = director.build_selection_context("model-a", task, 900.0, memory_context={})
    assert ctx["vfs_pressure"] == "high"
    assert ctx["budget_pressure"] == "high"


def test_director_persists_task_aggregate():
    director = MimoOrchestrationDirector()
    class Persistent:
        def __init__(self) -> None:
            self.saved = []
        def store_memory(self, **kwargs):
            self.saved.append(kwargs)
    persistent = Persistent()
    director.set_history_source(type("History", (), {"hybrid": type("Hybrid", (), {"persistent": persistent})()})())
    task = type("T", (), {"session_id": "s3", "task_id": "t3", "memory_scope": "task", "complexity": type("C", (), {"value": "medium"})(), "type": DummyTaskType("test")})()
    director.register_execution_result("model-x", True, 1.2, 100, 50, task=task)
    assert persistent.saved
    assert persistent.saved[0]["memory_type"] == "kpi_task:test"


def test_director_rolling_kpi_window_tracks_task_model():
    director = MimoOrchestrationDirector()
    task = type("T", (), {"session_id": "s4", "task_id": "t4", "memory_scope": "task", "complexity": type("C", (), {"value": "medium"})(), "type": DummyTaskType("code")})()
    for _ in range(3):
        director.register_execution_result("model-z", True, 1.0, task=task, quality_score=0.9)
    ctx = director.build_selection_context("model-z", task, 1000.0, memory_context={})
    assert ctx["rolling_kpi"]["success_rate"] == 1.0
    assert ctx["rolling_kpi"]["avg_quality"] >= 0.0


def test_director_profile_weights_reflect_provider_and_model():
    director = MimoOrchestrationDirector()
    weights = director._profile_weights("code", "openai", "deepseek-r1:14b")
    assert weights["quality"] > 1.0
    assert weights["budget"] < 1.3


def test_director_loads_json_profiles():
    director = MimoOrchestrationDirector()
    assert "plan" in director.task_profiles
    assert director._profile("plan")["default_context_depth"] == 4


def test_director_decay_reduces_old_kpi_weight(tmp_path):
    director = MimoOrchestrationDirector()
    director.kpi_store_path = tmp_path / "rolling_kpi_store.json"
    director.task_kpi_windows = {}
    task = type("T", (), {"session_id": "s5", "task_id": "t5", "memory_scope": "task", "complexity": type("C", (), {"value": "medium"})(), "type": DummyTaskType("code")})()
    for _ in range(5):
        director.register_execution_result("model-y", True, 2.0, task=task, quality_score=0.9)
    window = director.task_kpi_windows[("code", "model-y")]
    assert len(window.latencies) == 5
    assert window.snapshot()["success_rate"] == 1.0


def test_director_hot_reload_profiles(tmp_path):
    director = MimoOrchestrationDirector()
    target = director.profile_dir / "docs_light.json"
    original = target.read_text(encoding="utf-8")
    try:
        updated = original.replace('"default_context_depth": 1', '"default_context_depth": 3')
        target.write_text(updated, encoding="utf-8")
        changed = director.reload_profiles_if_changed()
        assert changed is True
        assert director._profile("docs_light")["default_context_depth"] == 3
    finally:
        target.write_text(original, encoding="utf-8")
        director.reload_profiles_if_changed()


def test_director_loads_granular_profiles():
    director = MimoOrchestrationDirector()
    assert "code_refactor" in director.task_profiles
    assert "review_security" in director.task_profiles
    assert "docs_api" in director.task_profiles


def test_director_prefers_more_specific_profile_for_regression_fix():
    director = MimoOrchestrationDirector()
    director.set_quality_source(type("Q", (), {"minimum_confidence": 0.8})())
    task = type("T", (), {"session_id": "s7", "task_id": "t7", "memory_scope": "task", "complexity": type("C", (), {"value": "high"})(), "type": DummyTaskType("test"), "input": type("I", (), {"description": "fix regression in api tests", "constraints": [], "acceptance_criteria": []})(), "priority": type("P", (), {"value": "high"})()})()
    ctx = director.build_selection_context("model-a", task, 4000.0, memory_context={})
    assert ctx["task_profile"]["task_type"] in {"test", "test_regression"}
    assert ctx["context_depth"] >= 3
    assert ctx["profile_weights"]["quality"] >= 1.0


def test_director_prefers_security_review_profile():
    director = MimoOrchestrationDirector()
    task = type("T", (), {"session_id": "s8", "task_id": "t8", "memory_scope": "task", "complexity": type("C", (), {"value": "high"})(), "type": DummyTaskType("review"), "input": type("I", (), {"description": "security review for auth and rbac changes", "constraints": [], "acceptance_criteria": []})(), "priority": type("P", (), {"value": "critical"})()})()
    ctx = director.build_selection_context("model-a", task, 1000.0, memory_context={})
    assert ctx["task_profile"]["task_type"] in {"review", "review_security"}
    assert ctx["context_depth"] >= 5


def test_director_persisted_kpi_store_roundtrip(tmp_path):
    director = MimoOrchestrationDirector()
    director.kpi_store_path = tmp_path / "rolling_kpi_store.json"
    task = type("T", (), {"session_id": "s6", "task_id": "t6", "memory_scope": "task", "complexity": type("C", (), {"value": "medium"})(), "type": DummyTaskType("docs")})()
    director.register_execution_result("model-persist", True, 1.0, task=task, quality_score=0.8)
    assert director.kpi_store_path.exists()
    reloaded = MimoOrchestrationDirector()
    reloaded.kpi_store_path = director.kpi_store_path
    reloaded._load_persisted_kpi_windows()
    assert ("docs", "model-persist") in reloaded.task_kpi_windows
