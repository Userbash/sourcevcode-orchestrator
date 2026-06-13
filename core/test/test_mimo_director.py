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


def test_director_exposes_runtime_health_and_selection_trace():
    director = MimoOrchestrationDirector()
    director.is_available = False
    task = type("T", (), {"session_id": "s8", "task_id": "t8", "memory_scope": "task", "complexity": type("C", (), {"value": "medium"})(), "type": DummyTaskType("docs"), "input": type("I", (), {"description": "docs update for api reference", "constraints": [], "acceptance_criteria": []})(), "priority": type("P", (), {"value": "normal"})()})()
    ctx = director.build_selection_context("model-a", task, 1000.0, memory_context={})

    assert ctx["mimo_runtime_health"]["ready"] is False
    assert ctx["mimo_runtime_health"]["profiles_loaded"] >= 1
    assert ctx["selection_trace"][0]["event"] == "runtime_health"
    assert any(item["event"] == "profile_selected" for item in ctx["selection_trace"])


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


import asyncio

from core.mimo.bridge import MimoAsyncBridge, MimoModelSnapshot


def test_mimo_bridge_ping_model_falls_back_to_cached_models():
    bridge = MimoAsyncBridge()
    bridge._cached_models = [
        MimoModelSnapshot(
            full_id="xiaomi/mimo-v2-pro",
            id="mimo-v2-pro",
            provider="xiaomi",
            status="active",
            context_window=1048576,
        )
    ]

    assert asyncio.run(bridge.ping_model("xiaomi/mimo-v2-pro")) is True
    assert asyncio.run(bridge.ping_model("mimo-v2-pro")) is True
    assert asyncio.run(bridge.ping_model("missing-model")) is False



def test_director_recommend_model_uses_safe_fallback_when_unavailable():
    director = MimoOrchestrationDirector()
    director.is_available = False
    task = type("T", (), {
        "session_id": "s9",
        "task_id": "t9",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "medium"})(),
        "type": DummyTaskType("docs"),
        "input": type("I", (), {"description": "docs update for api reference", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "normal"})(),
    })()

    recommendation = director.recommend_model(task, {"local_llm": {"ready": True}}, current_budget=1000.0)

    assert recommendation.allow is True
    assert recommendation.decision_mode == "safe_fallback"
    assert recommendation.provider == "local"
    assert recommendation.model_name == "qwen-2.5-7b-instruct"
    assert recommendation.reason == "mimo_unavailable_safe_fallback"


def test_director_recommend_model_blocks_unhealthy_provider():
    director = MimoOrchestrationDirector()
    director.is_available = True
    director.set_status_source(lambda: {"providers": {"antigravity": {"ready": False, "status": "degraded", "error": "auth_failed"}}})
    task = type("T", (), {
        "session_id": "s10",
        "task_id": "t10",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "medium"})(),
        "type": DummyTaskType("research"),
        "input": type("I", (), {"description": "compare options and investigate provider behavior", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "normal"})(),
    })()

    recommendation = director.recommend_model(
        task,
        {
            "selected_provider": "antigravity",
            "selected_model": "antigravity-cli",
        },
        current_budget=1000.0,
    )

    assert recommendation.allow is False
    assert recommendation.requires_escalation is True
    assert recommendation.blocked_by == "health"
    assert recommendation.provider == "antigravity"
    assert recommendation.model_name == "antigravity-cli"
    assert recommendation.fallback_options


def test_director_recommend_model_prefers_local_llm_owner_for_low_medium_docs():
    director = MimoOrchestrationDirector()
    director.is_available = True
    task = type("T", (), {
        "session_id": "s11",
        "task_id": "t11",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "medium"})(),
        "type": DummyTaskType("docs"),
        "input": type("I", (), {"description": "write concise docs summary", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "normal"})(),
    })()

    recommendation = director.recommend_model(
        task,
        {
            "local_llm": {
                "ready": True,
                "recommended_owner": "local_llm",
                "recommended_model": "qwen-2.5-7b-instruct",
                "task_family": "docs_workflow",
            }
        },
        current_budget=1000.0,
    )

    assert recommendation.allow is True
    assert recommendation.provider == "local"
    assert recommendation.model_name == "qwen-2.5-7b-instruct"
    assert recommendation.reason.startswith("mimo_recommend_local_llm_owner")



def test_director_resolve_candidate_models_filters_blocked_and_unhealthy_provider():
    director = MimoOrchestrationDirector()
    director.is_available = True
    director.bridge._cached_models = [
        MimoModelSnapshot(
            full_id="local/qwen-2.5-7b-instruct",
            id="qwen-2.5-7b-instruct",
            provider="local",
            status="active",
            context_window=131072,
            capability_tags=["docs", "research"],
            cost_class="low",
            ready=True,
            blocked=False,
        ),
        MimoModelSnapshot(
            full_id="antigravity/antigravity-cli",
            id="antigravity-cli",
            provider="antigravity",
            status="active",
            context_window=1048576,
            capability_tags=["docs", "research"],
            cost_class="medium",
            ready=True,
            blocked=False,
        ),
        MimoModelSnapshot(
            full_id="local/qwen2.5-32b-instruct-q4_k_m",
            id="qwen2.5:32b-instruct-q4_k_m",
            provider="local",
            status="active",
            context_window=65536,
            capability_tags=["code"],
            cost_class="medium",
            ready=True,
            blocked=True,
        ),
    ]
    director.set_status_source(lambda: {"providers": {"antigravity": {"ready": False, "status": "degraded", "error": "auth_failed"}}})
    task = type("T", (), {
        "session_id": "s12",
        "task_id": "t12",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "medium"})(),
        "type": DummyTaskType("docs"),
        "input": type("I", (), {"description": "write docs summary", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "normal"})(),
    })()

    candidates = director.resolve_candidate_models(task, {"selected_provider": "antigravity"})

    assert [item["model_name"] for item in candidates] == ["qwen-2.5-7b-instruct"]
    assert candidates[0]["provider"] == "local"
    assert candidates[0]["source"] == "mimo_inventory"


def test_director_safe_fallback_blocks_high_risk_when_mimo_unavailable():
    director = MimoOrchestrationDirector()
    director.is_available = False
    task = type("T", (), {
        "session_id": "s13",
        "task_id": "t13",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "high"})(),
        "type": DummyTaskType("review"),
        "input": type("I", (), {"description": "security review for auth and rbac changes", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "high"})(),
    })()

    recommendation = director.recommend_model(task, {}, current_budget=1000.0)

    assert recommendation.allow is False
    assert recommendation.decision_mode == "safe_fallback"
    assert recommendation.blocked_by == "policy"
    assert recommendation.requires_escalation is True


def test_director_provider_health_uses_full_provider_snapshot_and_local_advisory():
    director = MimoOrchestrationDirector()
    director.set_status_source(lambda: {
        "providers": {
            "openai": {"ready": True, "status": "healthy"},
            "mistral": {"ready": False, "status": "degraded", "error": "quota"},
        }
    })

    health = director._provider_health({"local_llm": {"ready": True, "status": "ready"}})

    assert health["openai"]["ready"] is True
    assert health["mistral"]["error"] == "quota"
    assert health["local"]["ready"] is True



def test_director_provider_health_accepts_availability_cached_report_shape():
    director = MimoOrchestrationDirector()
    director.set_status_source(lambda: {
        "providers": {
            "openai": {"provider": "openai", "status": "healthy", "latency_ms": 10.0, "error": None, "diagnostics": {}},
            "antigravity": {"provider": "antigravity", "status": "degraded", "latency_ms": 30.0, "error": "auth_failed", "diagnostics": {}},
        }
    })

    health = director._provider_health({})

    assert health["openai"]["ready"] is True
    assert health["antigravity"]["ready"] is False
    assert health["antigravity"]["error"] == "auth_failed"



def test_director_uses_local_llm_as_surrogate_controller_when_mimo_unavailable():
    director = MimoOrchestrationDirector()
    director.is_available = False
    task = type("T", (), {
        "session_id": "s14",
        "task_id": "t14",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "medium"})(),
        "type": DummyTaskType("docs"),
        "input": type("I", (), {"description": "write docs summary", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "normal"})(),
    })()

    recommendation = director.recommend_model(
        task,
        {
            "local_llm": {
                "ready": True,
                "recommended_owner": "local_llm",
                "recommended_model": "qwen-2.5-7b-instruct",
                "task_family": "docs_workflow",
            }
        },
        current_budget=1000.0,
    )

    assert recommendation.allow is True
    assert recommendation.decision_mode == "surrogate_controller"
    assert recommendation.provider == "local"
    assert recommendation.model_name == "qwen-2.5-7b-instruct"
    assert recommendation.reason == "local_llm_surrogate_controller"


def test_director_blocks_high_risk_even_with_local_llm_surrogate():
    director = MimoOrchestrationDirector()
    director.is_available = False
    task = type("T", (), {
        "session_id": "s15",
        "task_id": "t15",
        "memory_scope": "task",
        "complexity": type("C", (), {"value": "high"})(),
        "type": DummyTaskType("review"),
        "input": type("I", (), {"description": "security review for auth and rbac changes", "constraints": [], "acceptance_criteria": [], "files": []})(),
        "priority": type("P", (), {"value": "high"})(),
    })()

    recommendation = director.recommend_model(
        task,
        {
            "local_llm": {
                "ready": True,
                "recommended_owner": "local_llm",
                "recommended_model": "qwen-2.5-7b-instruct",
                "task_family": "review",
            }
        },
        current_budget=1000.0,
    )

    assert recommendation.allow is False
    assert recommendation.decision_mode == "safe_fallback"
    assert recommendation.requires_escalation is True
    assert recommendation.blocked_by == "policy"


def test_director_safe_sync_tracks_failure_reason_and_recovery_attempts(monkeypatch):
    director = MimoOrchestrationDirector()

    async def boom():
        raise RuntimeError("mimo_sync_failed")

    monkeypatch.setattr(director.bridge, "refresh_cache", boom)
    director.safe_sync()

    assert director.is_available is False
    assert director.last_failure_reason == "mimo_sync_failed"
    assert director.recovery_attempts == 1
