from __future__ import annotations

from types import SimpleNamespace

from core.core.local_llm_module import LocalLLMModule
from core.core.orchestrator import Orchestrator


class _Console:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, tag: str, message: str) -> None:
        self.messages.append((tag, message))

    def warning(self, tag: str, message: str) -> None:
        self.messages.append((tag, message))

    def emit(self, tag: str, message: str) -> None:
        self.messages.append((tag, message))


def test_autostart_local_llm_invokes_bridge(monkeypatch):
    module = LocalLLMModule(model_name="qwen2.5:32b-instruct-q4_k_m")
    bridge_calls: list[str] = []

    def fake_ensure_ready(model_name: str) -> bool:
        bridge_calls.append(model_name)
        return True

    monkeypatch.setattr(Orchestrator, "_local_llm_autostart_enabled", staticmethod(lambda: True))
    monkeypatch.setattr(Orchestrator, "_testing_mode", staticmethod(lambda: False))
    monkeypatch.setenv("TESTING", "false")

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.console = _Console()
    orchestrator.module_manager = SimpleNamespace(get_module=lambda name: module)
    orchestrator.local_llm_bridge = SimpleNamespace(ensure_ready=fake_ensure_ready)
    orchestrator.log = Orchestrator.log.__get__(orchestrator, Orchestrator)

    Orchestrator._autostart_local_llm(orchestrator)

    assert bridge_calls == ["qwen2.5:32b-instruct-q4_k_m"]
    assert any("Autostart complete" in message for _, message in orchestrator.console.messages)





def test_local_llm_decomposition_task_plan_uses_draft(monkeypatch):
    from core.core.models import Complexity, Priority, Task, TaskContext, TaskInput, TaskType
    from core.core.task_decomposer import TaskDecomposer

    draft = {
        "status": "model",
        "layers": [
            {
                "name": "intake",
                "objective": "Normalize the request",
                "capability": "plan",
                "task_type": "plan",
                "dependencies": [],
                "sub_agents": ["planner"],
            },
            {
                "name": "verification",
                "objective": "Prepare test coverage",
                "capability": "test",
                "task_type": "test",
                "dependencies": ["intake"],
                "sub_agents": ["tester"],
            },
        ],
    }

    module = LocalLLMModule(model_name="qwen2.5:32b-instruct-q4_k_m")
    monkeypatch.setattr(module, "check_health", lambda: {"ok": True, "model_present": True, "status_code": 200, "available_models": [module.model_name]})
    monkeypatch.setattr(module, "build_decomposition_draft", lambda task, context=None: {**draft, "ready": True, "task_family": "planning", "actions": ["break down task"], "core_retained_actions": ["security enforcement"], "recommended_model": module.model_name, "summary": "split the work", "context_digest": "short", "next_steps": ["plan", "test"], "model_hint": module.model_name})

    task = Task(
        task_id="task-smoke",
        type=TaskType.PLAN,
        priority=Priority.NORMAL,
        complexity=Complexity.MEDIUM,
        input=TaskInput(
            "Create a plan and tests for local LLM startup",
            files=["core/core/local_llm_bridge.py"],
            acceptance_criteria=["plan and test tasks exist"],
        ),
        context=TaskContext("smoke", ".", "main"),
    )

    plan = TaskDecomposer().decompose(task, {"local_llm": module.build_decomposition_draft(task)})

    assert [atomic.type.value for atomic in plan.atomic_tasks] == ["plan", "test"]
    assert plan.atomic_tasks[1].dependencies == [plan.atomic_tasks[0].task_id]
    assert plan.atomic_tasks[0].routing_hints["source"] == "local_llm"

def test_local_llm_bridge_auto_provisions_missing_container(monkeypatch):
    from core.core.local_llm_bridge import LocalLLMBridge
    from types import SimpleNamespace

    bridge = LocalLLMBridge(container_name="ai-kernel-local", ollama_port=11434)
    calls: list[str] = []

    monkeypatch.setenv("AI_BRIDGE_LOCAL_LLM_AUTO_PROVISION", "true")
    monkeypatch.setattr(LocalLLMBridge, "container_exists", lambda self: False)
    monkeypatch.setattr(LocalLLMBridge, "_host_probe", lambda self: {"ok": True})
    monkeypatch.setattr(LocalLLMBridge, "is_model_downloaded", lambda self, model_name: True)
    monkeypatch.setattr(LocalLLMBridge, "_run", lambda self, args, check=False: SimpleNamespace(returncode=0, stdout="", stderr=""))

    monkeypatch.setattr("core.core.local_llm_bridge.deploy_local_llm.ensure_container", lambda name: calls.append(f"ensure:{name}"))
    monkeypatch.setattr("core.core.local_llm_bridge.deploy_local_llm.install_ollama", lambda name: calls.append(f"install:{name}"))
    monkeypatch.setattr("core.core.local_llm_bridge.deploy_local_llm.start_service", lambda name: calls.append(f"start:{name}"))

    assert bridge.ensure_ready("qwen2.5:32b-instruct-q4_k_m") is True
    assert calls == ["ensure:ai-kernel-local", "install:ai-kernel-local", "start:ai-kernel-local"]


def test_orchestrator_preserves_local_llm_advisory_flags_during_mimo_merge():
    advisory_context = {
        "local_llm": {
            "ready": False,
            "should_delegate": False,
            "task_family": "planning",
        }
    }
    selection_context = {
        "budget_pressure": "high",
        "context_depth": 4,
    }

    local_llm_context = dict(advisory_context.get("local_llm") or {})
    local_ready = local_llm_context.get("ready")
    local_should_delegate = local_llm_context.get("should_delegate")
    local_task_family = local_llm_context.get("task_family")
    local_llm_context.update(selection_context)
    if local_ready is not None:
        local_llm_context["ready"] = local_ready
    if local_should_delegate is not None:
        local_llm_context["should_delegate"] = local_should_delegate
    if local_task_family is not None:
        local_llm_context["task_family"] = local_task_family

    assert local_llm_context["ready"] is False
    assert local_llm_context["should_delegate"] is False
    assert local_llm_context["task_family"] == "planning"
    assert local_llm_context["budget_pressure"] == "high"



def test_orchestrator_select_model_choice_uses_mimo_safe_fallback(monkeypatch):
    from core.core.model_selector import ModelSelector
    from core.core.models import Complexity, Priority, Task, TaskContext, TaskInput, TaskType
    from core.mimo.proxy import MimoRecommendation

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.model_selector = ModelSelector()
    orchestrator.model_selector.classify = lambda task: Complexity.MEDIUM
    orchestrator.model_selector.select = lambda task, advisory_context=None: (_ for _ in ()).throw(AssertionError("selector.select should not be used in safe fallback"))
    orchestrator.mimo_director = SimpleNamespace(
        recommend_model=lambda task, advisory_context, current_budget, memory_context=None: MimoRecommendation(
            provider="local",
            model_name="qwen-2.5-7b-instruct",
            confidence=0.4,
            allow=True,
            reason="mimo_unavailable_safe_fallback",
            decision_mode="safe_fallback",
        )
    )

    task = Task(
        TaskType.DOCS,
        TaskInput("write docs summary"),
        TaskContext("demo", ".", "main"),
        Priority.NORMAL,
    )

    choice, recommendation = Orchestrator._select_model_choice_with_mimo(orchestrator, task, {"local_llm": {}}, 1000.0, {})

    assert recommendation.decision_mode == "safe_fallback"
    assert choice.provider == "local"
    assert choice.model_name == "qwen-2.5-7b-instruct"
    assert choice.reason == "mimo_unavailable_safe_fallback"


def test_orchestrator_select_model_choice_returns_none_when_mimo_blocks(monkeypatch):
    from core.core.model_selector import ModelSelector
    from core.core.models import Priority, Task, TaskContext, TaskInput, TaskType
    from core.mimo.proxy import MimoRecommendation

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.model_selector = ModelSelector()
    orchestrator.model_selector.classify = lambda task: task.complexity
    orchestrator.model_selector.select = lambda task, advisory_context=None: (_ for _ in ()).throw(AssertionError("selector.select should not be used when mimo blocks"))
    orchestrator.mimo_director = SimpleNamespace(
        recommend_model=lambda task, advisory_context, current_budget, memory_context=None: MimoRecommendation(
            provider="antigravity",
            model_name="antigravity-cli",
            confidence=0.2,
            allow=False,
            reason="mimo_recommendation_blocked_provider_health",
            blocked_by="health",
            requires_escalation=True,
            escalation_reason="auth_failed",
        )
    )

    task = Task(
        TaskType.RESEARCH,
        TaskInput("investigate provider behavior"),
        TaskContext("demo", ".", "main"),
        Priority.NORMAL,
    )

    choice, recommendation = Orchestrator._select_model_choice_with_mimo(orchestrator, task, {}, 1000.0, {})

    assert choice is None
    assert recommendation.allow is False
    assert recommendation.blocked_by == "health"



def test_orchestrator_provider_health_snapshot_uses_availability_cache_and_antigravity_module():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.availability = SimpleNamespace(cached_report=lambda: {
        "openai": {"provider": "openai", "status": "healthy", "latency_ms": 10.0, "error": None, "diagnostics": {}},
        "mistral": {"provider": "mistral", "status": "degraded", "latency_ms": 20.0, "error": "quota", "diagnostics": {}},
    })
    orchestrator.module_manager = SimpleNamespace(get_module=lambda name: SimpleNamespace(snapshot=lambda: {"ready": True, "status": "ready"}) if name == "antigravity_status" else None)

    snapshot = Orchestrator._provider_health_snapshot(orchestrator)

    assert snapshot["providers"]["openai"]["status"] == "healthy"
    assert snapshot["providers"]["mistral"]["error"] == "quota"
    assert snapshot["providers"]["antigravity"]["ready"] is True



def test_orchestrator_select_model_choice_uses_surrogate_controller_without_selector(monkeypatch):
    from core.core.model_selector import ModelSelector
    from core.core.models import Complexity, Priority, Task, TaskContext, TaskInput, TaskType
    from core.mimo.proxy import MimoRecommendation

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.model_selector = ModelSelector()
    orchestrator.model_selector.classify = lambda task: Complexity.MEDIUM
    orchestrator.model_selector.select = lambda task, advisory_context=None: (_ for _ in ()).throw(AssertionError("selector.select should not be used in surrogate mode"))
    orchestrator.mimo_director = SimpleNamespace(
        recommend_model=lambda task, advisory_context, current_budget, memory_context=None: MimoRecommendation(
            provider="local",
            model_name="qwen-2.5-7b-instruct",
            confidence=0.72,
            allow=True,
            reason="local_llm_surrogate_controller",
            decision_mode="surrogate_controller",
        )
    )

    task = Task(
        TaskType.DOCS,
        TaskInput("write docs summary"),
        TaskContext("demo", ".", "main"),
        Priority.NORMAL,
    )

    choice, recommendation = Orchestrator._select_model_choice_with_mimo(orchestrator, task, {"local_llm": {}}, 1000.0, {})

    assert recommendation.decision_mode == "surrogate_controller"
    assert choice.provider == "local"
    assert choice.model_name == "qwen-2.5-7b-instruct"
    assert choice.reason == "local_llm_surrogate_controller"
