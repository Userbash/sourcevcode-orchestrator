from __future__ import annotations

from core.core.agent_registry import AgentRegistry
from core.core.model_selector import ModelChoice, ModelSelector
from core.core.models import Complexity, Priority, Task, TaskContext, TaskInput, TaskType
from core.core.provider_budget_router import ProviderBudgetRouter
from core.core.task_decomposer import TaskDecomposer


def _high_risk_code_task() -> Task:
    task = Task(
        TaskType.CODE,
        TaskInput(
            "Implement a production auth migration with rollback safety checks",
            files=["backend/auth.py", "backend/migrations/001_auth.sql"],
            acceptance_criteria=["migration is reversible", "tests pass"],
        ),
        TaskContext("demo", ".", "main"),
        priority=Priority.HIGH,
    )
    task.complexity = Complexity.HIGH
    task.routing_hints = {
        "normalized_text_profile": {
            "risk_bucket": "high",
            "decision_trust": "trusted",
            "execution_shape": "single_lane_validation",
            "confidence_score": 0.92,
        }
    }
    return task


def _with_socraticode_coverage(
    task: Task,
    *,
    coverage_ratio: float,
    coverage_status: str,
    prefer_low_cost: bool = False,
    preferred_provider: str | None = None,
    reduce_parallel_branches_to: int | None = None,
) -> Task:
    if not isinstance(task.routing_hints, dict):
        task.routing_hints = {}
    task.routing_hints["socraticode"] = {
        "context_coverage": {
            "ratio": coverage_ratio,
            "status": coverage_status,
        },
        "routing_recommendations": {
            "prefer_low_cost_lanes": prefer_low_cost,
            "reduce_parallel_branches_to": reduce_parallel_branches_to,
            "prefer_provider": preferred_provider,
        },
    }
    task.routing_hints["socraticode_cost_downgrade"] = {
        "eligible": prefer_low_cost,
        "preferred_provider": preferred_provider,
    }
    return task


class _StubAPI:
    def __init__(self, registry: AgentRegistry, local_agents: dict[str, object]) -> None:
        self.registry = registry
        self.local_agents = local_agents

    def get_module(self, name: str):
        return None


def _decomposer_with_code_agents(count: int) -> TaskDecomposer:
    registry = AgentRegistry()
    local_agents: dict[str, object] = {}
    providers = ["local", "mistral", "google", "openai"]
    models = ["local-small", "mistral-large-latest", "antigravity-cli", "gpt-5.5"]
    for idx in range(count):
        agent_id = f"code-{idx + 1}"
        registry.register(
            agent_id,
            "custom",
            f"local://{agent_id}",
            ["code"],
            model_name=models[idx % len(models)],
            provider=providers[idx % len(providers)],
        )
        local_agents[agent_id] = object()

    selector = ModelSelector()
    selector.set_api(_StubAPI(registry, local_agents))
    return TaskDecomposer(selector)


def _parallel_code_task(branches: int = 3, file_count: int = 2) -> Task:
    files = [f"service/file_{idx + 1}.py" for idx in range(file_count)]
    task = Task(
        TaskType.CODE,
        TaskInput(
            "Implement backend changes and frontend updates for the feature with tests aligned",
            files=files,
            acceptance_criteria=["backend updated", "frontend updated", "tests pass"],
        ),
        TaskContext("demo", ".", "main"),
    )
    task.routing_hints = {"parallelize_code": True, "parallel_branches": branches}
    return task


def test_low_socraticode_coverage_keeps_existing_provider_routing_intact():
    router = ProviderBudgetRouter()
    choice = ModelChoice(model_name="gpt-5.5", provider="openai", complexity=Complexity.HIGH)
    baseline_task = _high_risk_code_task()
    low_coverage_task = _with_socraticode_coverage(
        _high_risk_code_task(),
        coverage_ratio=0.28,
        coverage_status="low",
        prefer_low_cost=True,
    )

    baseline = router.preferred_providers(baseline_task, choice)
    low_coverage = router.preferred_providers(low_coverage_task, choice)

    assert baseline[0] == "openai"
    assert low_coverage == baseline


def test_high_socraticode_coverage_can_prefer_cheaper_provider_lanes():
    router = ProviderBudgetRouter()
    choice = ModelChoice(model_name="gpt-5.5", provider="openai", complexity=Complexity.HIGH)
    high_coverage_task = _with_socraticode_coverage(
        _high_risk_code_task(),
        coverage_ratio=0.94,
        coverage_status="strong",
        prefer_low_cost=True,
        preferred_provider="mistral",
    )

    ranked = router.preferred_providers(high_coverage_task, choice)

    assert ranked[0] in {"local", "mistral"}
    assert "openai" in ranked
    assert "local" in ranked
    assert "mistral" in ranked
    assert ranked.index("local") < ranked.index("openai")
    assert ranked.index("mistral") < ranked.index("openai")


def test_socraticode_preferred_provider_can_lead_ranking_when_not_critical():
    router = ProviderBudgetRouter()
    choice = ModelChoice(model_name="gpt-5.5", provider="openai", complexity=Complexity.MEDIUM)
    task = Task(TaskType.CODE, TaskInput("Refactor utility layer", files=["utils/a.py", "utils/b.py"]), TaskContext("demo", ".", "main"))
    task.complexity = Complexity.MEDIUM
    task.priority = Priority.NORMAL
    task.routing_hints = {}
    task = _with_socraticode_coverage(
        task,
        coverage_ratio=0.78,
        coverage_status="good",
        prefer_low_cost=True,
        preferred_provider="mistral",
    )

    ranked = router.preferred_providers(task, choice)

    assert ranked[0] == "mistral"
    assert ranked.index("mistral") < ranked.index("openai")


def test_low_socraticode_coverage_keeps_existing_parallel_fanout_shape():
    decomposer = _decomposer_with_code_agents(3)
    baseline_task = _parallel_code_task()
    low_coverage_task = _with_socraticode_coverage(
        _parallel_code_task(),
        coverage_ratio=0.31,
        coverage_status="low",
        reduce_parallel_branches_to=2,
    )

    baseline_plan = decomposer.decompose(baseline_task)
    low_coverage_plan = decomposer.decompose(low_coverage_task)

    baseline_code_tasks = [item for item in baseline_plan.atomic_tasks if item.type == TaskType.CODE]
    low_coverage_code_tasks = [item for item in low_coverage_plan.atomic_tasks if item.type == TaskType.CODE]

    assert len(baseline_code_tasks) == 3
    assert len(low_coverage_code_tasks) == len(baseline_code_tasks)


def test_high_socraticode_coverage_can_reduce_parallel_fanout():
    decomposer = _decomposer_with_code_agents(3)
    high_coverage_task = _with_socraticode_coverage(
        _parallel_code_task(),
        coverage_ratio=0.97,
        coverage_status="strong",
        reduce_parallel_branches_to=2,
    )

    plan = decomposer.decompose(high_coverage_task)
    code_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.CODE]
    review_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.REVIEW]

    assert len(code_tasks) == 2
    assert len(review_tasks) == 1
    assert set(review_tasks[0].dependencies) == {item.task_id for item in code_tasks}


def test_parallel_code_plan_can_scale_to_ten_agents_when_requested():
    decomposer = _decomposer_with_code_agents(10)
    task = _with_socraticode_coverage(
        _parallel_code_task(branches=10, file_count=10),
        coverage_ratio=0.98,
        coverage_status="strong",
        prefer_low_cost=True,
        reduce_parallel_branches_to=10,
    )

    plan = decomposer.decompose(task)
    code_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.CODE]
    review_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.REVIEW]

    assert len(code_tasks) == 10
    assert len(review_tasks) == 1
    assert len({item.routing_hints.get("preferred_agent_id") for item in code_tasks}) == 10
