from core.core.mistral_governance import MistralGovernance


class DummyTaskType:
    def __init__(self, value: str) -> None:
        self.value = value


class DummyPriority:
    def __init__(self, value: str) -> None:
        self.value = value


class DummyComplexity:
    def __init__(self, value: str) -> None:
        self.value = value


class DummyInput:
    def __init__(self, description: str, files=None, constraints=None, acceptance_criteria=None) -> None:
        self.description = description
        self.files = files or []
        self.constraints = constraints or []
        self.acceptance_criteria = acceptance_criteria or []


class DummyTask:
    def __init__(self, task_type: str, description: str, *, complexity: str = "medium", priority: str = "normal", files=None, constraints=None, acceptance_criteria=None) -> None:
        self.type = DummyTaskType(task_type)
        self.input = DummyInput(description, files=files, constraints=constraints, acceptance_criteria=acceptance_criteria)
        self.complexity = DummyComplexity(complexity)
        self.priority = DummyPriority(priority)


def test_governance_prefers_codestral_for_code_tasks():
    governance = MistralGovernance()
    task = DummyTask("code", "implement api client and refactor serializer", complexity="medium")

    profile = governance.build_profile(task, local_advisory={"ready": True}, current_budget=1000.0, provider_ready=True)

    assert profile["preferred_model"] == "codestral-latest"
    assert profile["selected_owner"] == "mistral"
    assert profile["management_profile"] == "coding_supervisor"


def test_governance_prefers_large_for_review_and_research():
    governance = MistralGovernance()
    task = DummyTask("review", "security review for auth and rbac changes", complexity="high", priority="high")

    profile = governance.build_profile(task, local_advisory={"ready": True}, current_budget=1000.0, provider_ready=True)

    assert profile["preferred_model"] == "mistral-large-latest"
    assert profile["selected_owner"] == "mistral_gateway"
    assert profile["authority_tier"] == "L3_gateway"


def test_governance_uses_mistral_as_gateway_for_local_llm_when_task_needs_supervision():
    governance = MistralGovernance()
    task = DummyTask(
        "plan",
        "break down a migration rollout and prepare docs, tests, and implementation plan",
        complexity="medium",
        acceptance_criteria=["plan backend steps", "plan tests", "plan docs"],
    )

    profile = governance.build_profile(
        task,
        local_advisory={"ready": True, "recommended_owner": "local_llm", "recommended_model": "qwen-2.5-7b-instruct"},
        current_budget=800.0,
        provider_ready=True,
    )

    assert profile["selected_owner"] == "mistral_gateway"
    assert profile["can_manage_local_llms"] is True
    assert profile["delegation_plan"][0]["delegate_to"] == "local_llm"
    assert len(profile["delegation_plan"]) >= 3


def test_governance_keeps_simple_low_docs_local_first():
    governance = MistralGovernance()
    task = DummyTask("docs", "summarize small api diff", complexity="low")

    profile = governance.build_profile(
        task,
        local_advisory={"ready": True, "recommended_owner": "local_llm", "recommended_model": "qwen-2.5-7b-instruct"},
        current_budget=800.0,
        provider_ready=True,
    )

    assert profile["selected_owner"] == "local_llm"
    assert profile["management_profile"] == "local_first"


def test_governance_cost_estimate_is_split_between_gateway_and_local_workers():
    governance = MistralGovernance()
    task = DummyTask("plan", "prepare release plan with test matrix and docs summary", complexity="medium")

    profile = governance.build_profile(task, local_advisory={"ready": True}, current_budget=500.0, provider_ready=True)
    estimate = profile["cost_estimate"]

    assert estimate["currency"] == "USD"
    assert estimate["gateway_usd"] > 0
    assert estimate["local_worker_usd"] == 0.0
    assert estimate["total_usd"] >= estimate["gateway_usd"]


def test_governance_breaks_task_into_simple_local_subtasks():
    governance = MistralGovernance()
    task = DummyTask(
        "research",
        "research architecture options and produce implementation, tests, and docs handoff",
        complexity="medium",
        files=["backend/api.py", "docs/README.md"],
        acceptance_criteria=["compare options", "prepare test plan", "draft docs summary"],
    )

    profile = governance.build_profile(task, local_advisory={"ready": True}, current_budget=500.0, provider_ready=True)

    delegates = profile["delegation_plan"]
    assert len(delegates) >= 3
    assert all(item["delegate_to"] == "local_llm" for item in delegates)
    assert any(item["task_type"] == "docs" for item in delegates)
    assert any(item["task_type"] == "test" for item in delegates)
