import asyncio
import json
import time
from core.agents.base_agent import BaseAgent
from core.agents.planner_agent import PlannerAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent
from core.core.models import AgentResult, Complexity, Priority, ResultOutput, Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.model_selector import ModelChoice
from core.core.orchestrator import Orchestrator
from core.core.availability import ProviderStatus


class LocalCodeAgent(BaseAgent):
    def __init__(self, agent_id: str = "code-1") -> None:
        super().__init__(agent_id, ["code", "fix", "refactor"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(task, "Implemented requested code changes.")


class FailingCodeAgent(BaseAgent):
    def __init__(self, agent_id: str = "code-failing") -> None:
        super().__init__(agent_id, ["code"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(task, "Implementation failed tests.", TaskStatus.FAILED, confidence=0.2, errors=["tests failed"])


class FixAgent(BaseAgent):
    def __init__(self, agent_id: str = "fix-1") -> None:
        super().__init__(agent_id, ["fix"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(task, "Fixed failed implementation and reran tests.")


class ResearchAgent(BaseAgent):
    def __init__(self, agent_id: str = "research-1") -> None:
        super().__init__(agent_id, ["research"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(task, "Collected supporting context and references.")


class DocsAgent(BaseAgent):
    def __init__(self, agent_id: str = "docs-1") -> None:
        super().__init__(agent_id, ["docs"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(task, "Prepared required documentation updates.")


class GatewayFailingAgent(BaseAgent):
    def __init__(self, agent_id: str = "gateway-failing") -> None:
        super().__init__(agent_id, ["docs", "research", "review", "code"])

    def run(self, task: Task, memory_context: dict | None = None):
        return self.result(
            task,
            "Remote provider disconnected.",
            TaskStatus.FAILED,
            confidence=0.1,
            errors=["unexpected status 502 Bad Gateway: service temporarily unavailable; stream disconnected before completion"],
        )


def _orchestrator_with_agents(code_agent: BaseAgent | None = None, fix_agent: BaseAgent | None = None) -> Orchestrator:
    orchestrator = Orchestrator()
    orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"))
    orchestrator.attach_local_agent("code-main", code_agent or LocalCodeAgent("code-main"))
    orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"))
    orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"))
    orchestrator.attach_local_agent("research-1", ResearchAgent("research-1"))
    orchestrator.attach_local_agent("docs-1", DocsAgent("docs-1"))
    if fix_agent:
        orchestrator.attach_local_agent("fix-1", fix_agent)
    return orchestrator


def test_full_cycle_plan_code_test_review_done():
    orchestrator = _orchestrator_with_agents()

    task = Task(TaskType.PLAN, TaskInput("Build feature", acceptance_criteria=["tests pass"]), TaskContext("demo", ".", "main"))
    result = asyncio.run(orchestrator.run(task))

    assert result["status"] == "done"
    assert result["merged"]["status"] == "done"
    assert result["results"]
    assert all(item["status"] == "done" for item in result["results"])
    assert any("[DONE]" in event for event in result["console"])
    assert "agents" in result["metrics"]


def test_full_cycle_delegates_failed_code_to_fix_agent_and_finishes():
    orchestrator = _orchestrator_with_agents(FailingCodeAgent("code-main"), FixAgent("fix-1"))

    task = Task(TaskType.PLAN, TaskInput("Build feature with a failing first implementation", acceptance_criteria=["tests pass"]), TaskContext("demo", ".", "main"))
    result = asyncio.run(orchestrator.run(task))

    assert result["status"] == "done"
    assert any(item["agent_id"] == "fix-1" and item["status"] == "done" for item in result["results"])
    assert any("[FIX]" in event for event in result["console"])
    assert any(row.get("router_agent") == "fix-1" for row in result["live_trace"])


def test_dependency_handoff_dispatches_p2p_context_to_next_agent():
    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("code-alt", LocalCodeAgent("code-alt"), model_name="antigravity-cli", provider="google")

    source = Task(TaskType.CODE, TaskInput("Implement branch A"), TaskContext("demo", ".", "main"))
    source.required_capability = "code"
    target = Task(TaskType.CODE, TaskInput("Implement branch B"), TaskContext("demo", ".", "main"), dependencies=[source.task_id])
    target.required_capability = "code"
    target.routing_hints = {"preferred_agent_id": "code-alt"}

    source_result = AgentResult(task_id=source.task_id, agent_id="code-main", status=TaskStatus.DONE, output=ResultOutput(summary="branch A done", files_changed=["a.py"]), confidence=0.9)

    count = orchestrator._dispatch_dependency_handoffs([target], {source.task_id: source_result})
    time.sleep(0.2)
    handoffs = orchestrator._consume_p2p_handoffs("code-alt", target.task_id)

    assert count == 1
    assert handoffs
    assert handoffs[0]["summary"] == "branch A done"
    assert handoffs[0]["from_agent"] == "code-main"


def test_parallel_batch_preassignment_spreads_code_tasks_across_agents():
    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("code-alt", LocalCodeAgent("code-alt"))

    first = Task(TaskType.CODE, TaskInput("Implement branch A"), TaskContext("demo", ".", "main"))
    first.required_capability = "code"
    second = Task(TaskType.CODE, TaskInput("Implement branch B"), TaskContext("demo", ".", "main"))
    second.required_capability = "code"

    assignments = orchestrator._preassign_parallel_batch_agents([first, second])

    assert assignments[first.task_id] != assignments[second.task_id]
    assert {assignments[first.task_id], assignments[second.task_id]} == {"code-main", "code-alt"}


def test_parallel_batch_preassignment_prefers_distinct_models_when_agents_overlap():
    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("code-alt", LocalCodeAgent("code-alt"), model_name="antigravity-cli", provider="google")
    orchestrator.attach_local_agent("code-third", LocalCodeAgent("code-third"), model_name="local-small", provider="local")

    first = Task(TaskType.CODE, TaskInput("Implement branch A"), TaskContext("demo", ".", "main"))
    first.required_capability = "code"
    second = Task(TaskType.CODE, TaskInput("Implement branch B"), TaskContext("demo", ".", "main"))
    second.required_capability = "code"

    assignments = orchestrator._preassign_parallel_batch_agents([first, second])

    assert assignments[first.task_id] != assignments[second.task_id]
    chosen = {assignments[first.task_id], assignments[second.task_id]}
    assert chosen == {"code-main", "code-alt"}



def test_code_task_decomposition_can_fan_out_across_multiple_ai_agents():
    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("code-alt", LocalCodeAgent("code-alt"), model_name="antigravity-cli", provider="google")
    orchestrator.attach_local_agent("code-third", LocalCodeAgent("code-third"), model_name="mistral-large-latest", provider="mistral")

    task = Task(
        TaskType.CODE,
        TaskInput(
            "Implement backend changes and frontend updates for the feature with tests aligned",
            files=["backend/app.py", "frontend/ui.tsx"],
            acceptance_criteria=["backend updated", "frontend updated"],
        ),
        TaskContext("demo", ".", "main"),
    )
    task.routing_hints = {"parallelize_code": True, "parallel_branches": 3}

    plan = orchestrator.decomposer.decompose(task)

    code_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.CODE]
    review_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.REVIEW]

    assert len(code_tasks) == 3
    assert len(review_tasks) == 1
    assert {item.routing_hints.get("preferred_agent_id") for item in code_tasks} == {"code-main", "code-alt", "code-third"}
    assert set(review_tasks[0].dependencies) == {item.task_id for item in code_tasks}


def test_distribution_trace_shows_pipeline_and_agent_assignment():
    orchestrator = _orchestrator_with_agents()

    task = Task(
        TaskType.PLAN,
        TaskInput(
            "Build feature with backend, tests, and docs",
            files=["core/app.py", "tests/test_app.py"],
            acceptance_criteria=["backend works", "tests pass", "docs updated"],
        ),
        TaskContext("demo", ".", "main"),
    )

    result = asyncio.run(orchestrator.run(task))

    assert result["status"] == "done"
    assert result["results"]

    # Process visibility: root task -> decomposition -> routing -> specialist execution.
    task_types = {item["task_type"] for item in result["results"]}
    assigned_agents = {item["agent_id"] for item in result["results"] if item.get("agent_id")}
    trace_pairs = {(row.get("task_type"), row.get("router_agent"), row.get("selected_provider")) for row in result["live_trace"]}

    assert {"plan", "code", "test", "review"}.issubset(task_types)
    assert {"planner-1", "code-main", "tester-1", "reviewer-1"}.issubset(assigned_agents)
    assert any(task_type == "plan" for task_type, _, _ in trace_pairs)
    assert any(task_type == "code" for task_type, _, _ in trace_pairs)
    assert any(task_type == "test" for task_type, _, _ in trace_pairs)
    assert any(task_type == "review" for task_type, _, _ in trace_pairs)

    # The trace should also preserve model/provider attribution for debugging.
    for row in result["live_trace"]:
        assert row.get("selected_provider")
        assert row.get("selected_model")


def test_feedback_loop_does_not_recurse_fix_tasks():
    from core.core.feedback_loop import FeedbackLoop
    from core.core.models import Priority

    feedback = FeedbackLoop(retry_limit=1)
    task = Task(TaskType.PLAN, TaskInput("broken"), TaskContext("demo", ".", "main"), priority=Priority.NORMAL)
    result = AgentResult(task_id=task.task_id, agent_id="agent", status=TaskStatus.FAILED, output=ResultOutput(summary="bad"), confidence=0.1, errors=["bad"], next_recommendations=[], provider="local", model_name="local-small")

    ok, fix_task = feedback.evaluate(task, result)
    assert not ok
    assert fix_task is not None
    assert fix_task.parent_task_id == task.task_id
    assert fix_task.retry_count == 1

    fix_result = AgentResult(task_id=fix_task.task_id, agent_id="agent", status=TaskStatus.FAILED, output=ResultOutput(summary="still bad"), confidence=0.1, errors=["bad"], next_recommendations=[], provider="local", model_name="local-small")
    ok, nested_fix = feedback.evaluate(fix_task, fix_result)
    assert not ok
    assert nested_fix is None



def test_code_task_decomposition_uses_normalized_profile_for_parallel_fanout():
    from core.core.task_submission_api import create_standard_task

    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("code-alt", LocalCodeAgent("code-alt"), model_name="antigravity-cli", provider="google")
    orchestrator.attach_local_agent("code-third", LocalCodeAgent("code-third"), model_name="mistral-large-latest", provider="mistral")

    task = create_standard_task({
        "message": "Implement backend and frontend changes for the feature and add tests",
        "files": "backend/app.py\nfrontend/ui.tsx",
        "acceptance_criteria": "backend updated\nfrontend updated\ntests pass",
        "type": "code",
    })

    plan = orchestrator.decomposer.decompose(task)

    code_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.CODE]
    assert len(code_tasks) == 3
    assert all(item.routing_hints.get("preferred_agent_id") for item in code_tasks)


def test_parallel_code_plan_applies_openai_template_hints():
    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("code-alt", LocalCodeAgent("code-alt"), model_name="antigravity-cli", provider="google")
    orchestrator.attach_local_agent("code-third", LocalCodeAgent("code-third"), model_name="mistral-large-latest", provider="mistral")

    task = Task(
        TaskType.CODE,
        TaskInput(
            "Implement backend changes and frontend updates for the feature with tests aligned",
            files=["backend/app.py", "frontend/ui.tsx"],
            acceptance_criteria=["backend updated", "frontend updated"],
        ),
        TaskContext("demo", ".", "main"),
    )
    task.routing_hints = {"parallelize_code": True, "parallel_branches": 3}

    advisory = {
        "openai_compatible": {
            "code_parallel_candidates": [
                {"model_name": "gpt-5.5", "provider": "openai", "role": "code_parallel", "family": "gpt", "tier": "frontier"},
                {"model_name": "claude-sonnet-4-6", "provider": "openai", "role": "code_parallel", "family": "claude", "tier": "frontier"},
                {"model_name": "deepseek-v4-pro", "provider": "openai", "role": "code_parallel", "family": "deepseek", "tier": "frontier"},
            ],
            "review_candidates": [
                {"model_name": "claude-opus-4-8", "provider": "openai", "role": "review_primary", "family": "claude", "tier": "frontier"},
            ],
        }
    }

    plan = orchestrator.decomposer.decompose(task, advisory_context=advisory)

    code_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.CODE]
    review_tasks = [item for item in plan.atomic_tasks if item.type == TaskType.REVIEW]

    assert [item.assigned_model for item in code_tasks] == ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-pro"]
    assert all(item.routing_hints.get("preferred_provider") == "openai" for item in code_tasks)
    assert review_tasks[0].assigned_model == "claude-opus-4-8"
    assert review_tasks[0].routing_hints.get("model_template_role") == "review_primary"


def test_sync_openai_template_workers_attaches_and_prunes(tmp_path, monkeypatch):
    catalog = tmp_path / "orchestrator_templates.json"
    monkeypatch.setenv("OPENAI_ORCHESTRATOR_TEMPLATES_PATH", str(catalog))

    catalog.write_text(json.dumps({
        "roles": {
            "code_parallel": [
                {"model_name": "gpt-5.5"},
                {"model_name": "claude-sonnet-4-6"},
                {"model_name": "deepseek-v4-pro"},
            ]
        }
    }), encoding="utf-8")

    orchestrator = _orchestrator_with_agents()
    for agent_id in [item for item in list(orchestrator.local_agents) if item.startswith("codex-openai-")]:
        orchestrator._detach_local_agent(agent_id)
    orchestrator._openai_template_agent_ids = set()
    catalog.write_text(json.dumps({
        "roles": {
            "code_parallel": [
                {"model_name": "gpt-5.5"},
                {"model_name": "claude-sonnet-4-6"},
                {"model_name": "deepseek-v4-pro"},
            ]
        }
    }), encoding="utf-8")
    sync = orchestrator.sync_openai_template_workers(enabled=True, primary_model="gpt-5.5")

    assert set(sync["attached"]) == {"codex-openai-claude-sonnet-4-6", "codex-openai-deepseek-v4-pro"}
    assert orchestrator.registry.get("codex-openai-claude-sonnet-4-6") is not None
    assert orchestrator.registry.get("codex-openai-deepseek-v4-pro") is not None

    catalog.write_text(json.dumps({
        "roles": {
            "code_parallel": [
                {"model_name": "gpt-5.5"},
                {"model_name": "qwen3.7-max"},
            ]
        }
    }), encoding="utf-8")

    resync = orchestrator.sync_openai_template_workers(enabled=True, primary_model="gpt-5.5")

    assert set(resync["removed"]) == {"codex-openai-claude-sonnet-4-6", "codex-openai-deepseek-v4-pro"}
    assert resync["attached"] == ["codex-openai-qwen3-7-max"]
    assert orchestrator.registry.get("codex-openai-claude-sonnet-4-6") is None
    assert orchestrator.registry.get("codex-openai-deepseek-v4-pro") is None
    assert orchestrator.registry.get("codex-openai-qwen3-7-max") is not None


def test_refresh_provider_inventory_snapshot_records_worker_sync(monkeypatch):
    orchestrator = _orchestrator_with_agents()

    calls = {}

    def _fake_refresh(force_refresh=False):
        return {"openai": {"ok": True, "diagnostics": {}}, "mistral": {"ok": True}}

    def _fake_participation(records):
        return {"agent_count": len(list(records))}

    def _fake_sync(*, enabled=True, primary_model=""):
        calls["enabled"] = enabled
        calls["primary_model"] = primary_model
        return {"attached": ["codex-openai-claude-sonnet-4-6"], "removed": [], "kept": [], "enabled": enabled}

    monkeypatch.setattr(orchestrator.provider_inventory, "refresh", _fake_refresh)
    monkeypatch.setattr(orchestrator.provider_inventory, "build_participation_snapshot", _fake_participation)
    monkeypatch.setattr(orchestrator, "sync_openai_template_workers", _fake_sync)

    snapshot = orchestrator._refresh_provider_inventory_snapshot(force_refresh=True)

    assert calls["enabled"] is True
    assert snapshot["providers"]["openai"]["diagnostics"]["worker_sync"]["attached"] == ["codex-openai-claude-sonnet-4-6"]
    assert snapshot["participation"]["agent_count"] >= 1



def test_refresh_provider_inventory_snapshot_suppresses_failed_provider(monkeypatch):
    orchestrator = _orchestrator_with_agents()
    orchestrator.provider_budget_router.suppress_provider('openai', seconds=60, reason='stale')

    def _fake_refresh(force_refresh=False):
        return {
            "openai": {"ok": True, "diagnostics": {}},
            "mimo": {"ok": False, "error": "401 invalid api key", "diagnostics": {}},
        }

    def _fake_participation(records):
        return {
            "active_now": [{"provider": "openai", "model_name": "gpt-5.5", "source": "registered_agent"}],
            "available_but_not_wired_directly": [],
            "present_but_unusable": [{"provider": "mimo", "model_name": "mimo/mimo-auto", "reason": "github_pat_not_supported"}],
        }

    monkeypatch.setattr(orchestrator.provider_inventory, "refresh", _fake_refresh)
    monkeypatch.setattr(orchestrator.provider_inventory, "build_participation_snapshot", _fake_participation)
    monkeypatch.setattr(orchestrator, "sync_openai_template_workers", lambda **kwargs: {"attached": [], "removed": [], "kept": [], "enabled": True})
    monkeypatch.setattr(orchestrator.availability, "cached_report", lambda: {
        "mimo": {"status": "auth_failed", "error": "auth_failed"},
        "openai": {"status": "healthy", "error": None},
    })

    snapshot = orchestrator._refresh_provider_inventory_snapshot(force_refresh=True)

    assert 'mimo' in snapshot['provider_suppression']['suppressed']
    assert snapshot['provider_suppression']['suppressed']['mimo']['error_type'] == 'auth_fail'
    assert 'mimo' in snapshot['provider_budget_router']['global_suppression']
    assert 'openai' not in snapshot['provider_budget_router']['global_suppression']

def test_orchestrator_selects_mimo_agent_for_xiaomi_provider_alias():
    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("mimo-router-1", LocalCodeAgent("mimo-router-1"), model_name="mimo/mimo-auto", provider="mimo")

    selected = orchestrator._select_agent_by_provider_preference("code", ["xiaomi"])

    assert selected == "mimo-router-1"


def test_orchestrator_normalizes_github_models_to_mimo():
    assert Orchestrator._normalize_provider("github-models") == "mimo"


def test_orchestrator_runtime_gateway_failure_retries_via_delivery_path(monkeypatch):
    orchestrator = _orchestrator_with_agents()
    orchestrator.attach_local_agent("docs-remote", GatewayFailingAgent("docs-remote"), provider="openai", model_name="gpt-5.5")
    orchestrator.attach_local_agent("docs-local", DocsAgent("docs-local"), provider="local", model_name="qwen-2.5-7b-instruct")

    calls: list[tuple[str, str]] = []
    real_run_via_delivery = orchestrator._run_local_agent_via_delivery

    def _spy_run_via_delivery(task, agent_id, capability, agent, memory_context):
        calls.append((agent_id, capability))
        return real_run_via_delivery(task, agent_id, capability, agent, memory_context)

    class _Health:
        def __init__(self, provider: str):
            self.provider = provider
            self.status = ProviderStatus.HEALTHY
            self.error = None
        def as_dict(self):
            return {"provider": self.provider, "status": "healthy", "error": None, "diagnostics": {}}

    monkeypatch.setattr(orchestrator, "_select_model_choice_with_mimo", lambda *args, **kwargs: (ModelChoice(
        model_name="gpt-5.5",
        provider="openai",
        complexity=Complexity.MEDIUM,
        requires_secondary_review=False,
        reason="test_choice",
        detected_keywords=[],
        matched_high_risk_rules=[],
        matched_low_risk_exemptions=[],
    ), None))
    monkeypatch.setattr(orchestrator, "_build_decomposition_advisory", lambda task: {"local_llm": {"ready": True, "should_delegate": True, "task_family": "docs"}})
    monkeypatch.setattr(orchestrator.availability, "check_provider", lambda provider, live=True: _Health(provider))
    monkeypatch.setattr(orchestrator, "_run_local_agent_via_delivery", _spy_run_via_delivery)

    task = Task(TaskType.DOCS, TaskInput("Explain fallback behavior"), TaskContext("demo", ".", "main"), priority=Priority.NORMAL)
    task.required_capability = "docs"
    task.routing_hints = {"provider_preference": "openai", "source": "websocket", "cost_tier": "interactive"}

    result = orchestrator.run_task(task)

    assert result.status == TaskStatus.DONE
    assert calls[0][0] == "docs-remote"
    assert calls[-1][0] == "docs-local"
    assert len(calls) >= 2
