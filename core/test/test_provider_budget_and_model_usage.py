from __future__ import annotations

from core.agents.codex_agent import CodexAgent
from core.agents.gemini_cli_agent import GeminiCLIAgent
from core.agents.planner_agent import PlannerAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent
from core.core.models import AgentResult, Priority, Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.orchestrator import Orchestrator
from core.core.provider_budget_router import ProviderBudgetRouter
from core.core.model_usage_module import ModelUsageModule
from core.core.security import SecurityManager, SecurityPolicy
from core.core.task_decomposer import TaskDecomposer


def test_decomposer_sets_code_test_normal_and_review_high():
    root = Task(
        TaskType.PLAN,
        TaskInput("Improve orchestrator and monitoring"),
        TaskContext("demo", ".", "main"),
        priority=Priority.HIGH,
    )
    plan = TaskDecomposer().decompose(root)
    by_type = {t.type: t for t in plan.atomic_tasks}

    assert by_type[TaskType.PLAN].priority == Priority.HIGH
    assert by_type[TaskType.CODE].priority == Priority.NORMAL
    assert by_type[TaskType.TEST].priority == Priority.NORMAL
    assert by_type[TaskType.REVIEW].priority == Priority.HIGH


def test_provider_budget_router_prefers_primary_provider_for_normal_code():
    task = Task(
        TaskType.CODE,
        TaskInput("Implement feature"),
        TaskContext("demo", ".", "main"),
        priority=Priority.NORMAL,
    )
    router = ProviderBudgetRouter()
    class _Choice:
        provider = "mistral"
    providers = router.preferred_providers(task, _Choice())
    assert providers[0] == "mistral"


def test_provider_budget_router_normalizes_antigravity_aliases():
    router = ProviderBudgetRouter()

    class _Choice:
        provider = "google"

    task = Task(
        TaskType.DOCS,
        TaskInput("Write docs"),
        TaskContext("demo", ".", "main"),
        priority=Priority.NORMAL,
    )

    providers = router.preferred_providers(task, _Choice())

    assert providers[0] == "antigravity"


def test_orchestrator_exposes_model_usage_snapshot():
    orchestrator = Orchestrator()
    sec = SecurityManager(SecurityPolicy(allow_shell=True, shell_allowlist=["agy -p", "antigravity -p"]))

    orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"), agent_type="planner", provider="openai")
    orchestrator.attach_local_agent("codex-main", CodexAgent("codex-main"), agent_type="codex", provider="openai")
    orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"), agent_type="tester", provider="openai")
    orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"), agent_type="reviewer", provider="openai")
    orchestrator.attach_local_agent("antigravity-cli-1", GeminiCLIAgent("antigravity-cli-1", sec), agent_type="external_ai", provider="google")

    result = orchestrator.submit_user_task({"type": "plan", "description": "Small feature", "priority": "normal"}, source="test")

    assert "model_usage" in result
    assert "history" in result["model_usage"]
    assert len(result["model_usage"]["history"]) >= 1


def test_model_usage_budget_policy_uses_remaining_percentage_semantics():
    module = ModelUsageModule()
    module._model_limits = {"test-model": 1000}

    stat = module._get_or_create_stats("test-model")
    stat.used_tokens = 100
    healthy = module.evaluate_model_budget("test-model")
    assert healthy["remaining_percentage"] == 90.0
    assert healthy["action"] == "ok"

    stat.used_tokens = 960
    exhausted = module.evaluate_model_budget("test-model")
    assert exhausted["remaining_percentage"] == 4.0
    assert exhausted["action"] == "error"


def test_model_usage_requests_parallelism_reduction_when_remaining_budget_is_low():
    module = ModelUsageModule()
    module._model_limits = {"test-model": 1000}
    stat = module._get_or_create_stats("test-model")
    stat.used_tokens = 915

    policy = module.evaluate_model_budget("test-model")
    assert policy["remaining_percentage"] == 8.5
    assert policy["action"] == "reduce"
    assert module.should_reduce_parallelism() is True


def test_model_usage_estimates_mistral_gateway_costs():
    module = ModelUsageModule()
    estimate = module.estimate_usage_cost("mistral-large-latest", input_tokens=2000, output_tokens=1000)

    assert estimate["currency"] == "USD"
    assert estimate["provider"] == "mistral"
    assert estimate["estimated_cost_usd"] > 0


def test_model_usage_after_task_records_estimated_cost_for_mistral():
    module = ModelUsageModule()
    task = Task(
        TaskType.REVIEW,
        TaskInput("review backend auth changes"),
        TaskContext("demo", ".", "main"),
        priority=Priority.NORMAL,
    )
    context = {"model": "mistral-large-latest", "provider": "mistral", "usage_tokens": 500}
    module.before_task(task, context)
    result = AgentResult(task.task_id, "mistral-1", TaskStatus.DONE, {"summary": "ok"}, 0.9, [], [], provider="mistral", model_name="mistral-large-latest")

    module.after_task(task, result, context)

    assert module.history[-1]["provider"] == "mistral"
    assert module.history[-1]["estimated_cost_usd"] > 0



def test_model_usage_estimates_local_llm_costs_with_runtime_components(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_LOCAL_LLM_AMORTIZED_USD_PER_HOUR", "0.72")
    monkeypatch.setenv("AI_BRIDGE_LOCAL_LLM_OPERATIONS_USD_PER_HOUR", "0.11")
    module = ModelUsageModule()

    estimate = module.estimate_usage_cost(
        "qwen2.5:32b-instruct-q4_k_m",
        input_tokens=1800,
        output_tokens=600,
        provider="local",
        runtime={"latency_sec": 3.2, "gpu_time_sec": 2.7, "cpu_time_sec": 1.4, "ram_gb": 10.0},
    )

    assert estimate["provider"] == "local"
    assert estimate["estimated_cost_usd"] > 0
    assert estimate["cost_components"]["energy_usd"] > 0
    assert estimate["cost_components"]["amortized_hardware_usd"] > 0
    assert estimate["cost_components"]["memory_pressure_usd"] > 0


def test_model_usage_after_task_records_local_cost_components(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_LOCAL_LLM_REQUEST_OVERHEAD_USD", "0.0003")
    module = ModelUsageModule()
    task = Task(
        TaskType.DOCS,
        TaskInput("summarize the release changes"),
        TaskContext("demo", ".", "main"),
        priority=Priority.NORMAL,
    )
    context = {"model": "qwen2.5:32b-instruct-q4_k_m", "provider": "local", "usage_tokens": 700}
    module.before_task(task, context)
    result = AgentResult(
        task.task_id,
        "local-llm-1",
        TaskStatus.DONE,
        {"summary": "ok", "local_usage": {"latency_sec": 2.5, "gpu_time_sec": 2.0, "cpu_time_sec": 1.2, "ram_gb": 8.0}},
        0.9,
        [],
        [],
        provider="local",
        model_name="qwen2.5:32b-instruct-q4_k_m",
    )

    module.after_task(task, result, context)

    assert module.history[-1]["provider"] == "local"
    assert module.history[-1]["estimated_cost_usd"] > 0
    assert module.history[-1]["cost_components"]["latency_sec"] == 2.5


def test_provider_budget_router_honors_websocket_provider_preference():
    task = Task(
        TaskType.DOCS,
        TaskInput("Explain websocket routing"),
        TaskContext("demo", ".", "main"),
        priority=Priority.NORMAL,
    )
    task.routing_hints = {"source": "websocket", "provider_preference": "openai", "cost_tier": "interactive"}

    class _Choice:
        provider = "mistral"

    providers = ProviderBudgetRouter().preferred_providers(task, _Choice())

    assert providers[0] == "openai"


def test_provider_budget_router_uses_cheaper_order_for_websocket_economy():
    task = Task(
        TaskType.DOCS,
        TaskInput("Summarize release notes"),
        TaskContext("demo", ".", "main"),
        priority=Priority.NORMAL,
    )
    task.routing_hints = {"source": "websocket", "cost_tier": "economy"}

    class _Choice:
        provider = "openai"

    providers = ProviderBudgetRouter().preferred_providers(task, _Choice())

    assert providers[0] == "local"
