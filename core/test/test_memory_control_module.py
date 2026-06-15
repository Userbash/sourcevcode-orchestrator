from __future__ import annotations

from types import SimpleNamespace

from core.core.layered_context_memory import LayeredContextMemory
from core.core.memory_control_module import MemoryControlModule
from core.core.models import AgentResult, ExecutionPlan, ResultOutput, Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.session_memory import SessionMemory


class _FakeAPI:
    def __init__(self, **contexts):
        self._contexts = contexts

    def get_context(self, key: str):
        return self._contexts.get(key)

    def emit_event(self, event_name: str, payload: dict):
        return None

    def query_state(self, module_name: str, key: str):
        return None

    def log(self, level: str, message: str) -> None:
        return None

    def get_module(self, name: str):
        return None

    def load_module(self, name: str) -> None:
        return None

    def unload_module(self, name: str) -> None:
        return None


def _task(description: str = "Implement memory-aware code path") -> Task:
    task = Task(
        TaskType.CODE,
        TaskInput(
            description,
            files=["core/core/orchestrator.py"],
            constraints=["do not break handlers"],
            acceptance_criteria=["tests pass", "memory profile is applied"],
        ),
        TaskContext("demo", ".", "main"),
    )
    task.session_id = "sess-memory-control"
    task.required_capability = "code"
    return task


def test_memory_control_builds_provider_specific_runtime_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path))
    memory = SessionMemory()
    layered = LayeredContextMemory(memory)
    config = SimpleNamespace(high_risk_trained_memory_enabled=True)
    module = MemoryControlModule()
    module.on_load(_FakeAPI(session_memory=memory, layered_context_memory=layered, orchestration_config=config))

    task = _task()
    task.routing_hints = {"normalized_text_profile": {"execution_shape": "parallel_candidate", "risk_bucket": "high", "decision_trust": "trusted", "confidence_score": 0.86}}
    module.register_submission(task, raw_payload="build feature", normalized_payload={"description": task.input.description}, source="user")
    module.register_planning_draft(task, {"local_llm": {"summary": "First draft a plan, then decompose"}}, source="test")
    plan = ExecutionPlan(root_task_id=task.task_id, atomic_tasks=[task], draft_layers=[{"name": "code", "objective": task.input.description}])
    module.register_decomposition(task, plan, source="test")
    module.register_routing_outcome(
        task,
        selected_provider="antigravity",
        selected_model="gemini-3.5-flash",
        routed_agent="codex-main",
        routed_provider="antigravity",
        routed_model="gemini-3.5-flash",
        reason="policy",
        fallback_count=0,
    )
    result = AgentResult(
        task.task_id,
        "codex-main",
        TaskStatus.DONE,
        ResultOutput(summary="Implemented with explicit acceptance criteria"),
        0.94,
        [],
        [],
        provider="antigravity",
        model_name="gemini-3.5-flash",
    )
    module.register_result(task, result, quality_score=0.94, fallback_count=0, latency_ms=420.0)

    context = module.build_runtime_context(task, agent_id="codex-main", provider="antigravity", model_name="gemini-3.5-flash")

    assert context["memory_profile"] == "rich_synthesis"
    assert "layered_context_brief" in context
    assert "prompt_guidance" in context
    assert context["normalized_text_profile"]["execution_shape"] == "parallel_candidate"
    assert "normalization_guidance" in context
    assert module.finalize()["reads_total"] == 1


def test_memory_control_prepares_parallel_batch_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path))
    memory = SessionMemory()
    module = MemoryControlModule()
    module.on_load(_FakeAPI(session_memory=memory, layered_context_memory=memory.layered, orchestration_config=SimpleNamespace(high_risk_trained_memory_enabled=False)))

    first = _task("Implement branch A")
    second = _task("Implement branch B")
    second.session_id = "sess-memory-control-2"

    assignments = {first.task_id: "code-main", second.task_id: "code-alt"}
    registry = SimpleNamespace(
        get=lambda agent_id: {
            "code-main": SimpleNamespace(provider="antigravity", model_name="gemini-3.5-flash"),
            "code-alt": SimpleNamespace(provider="local", model_name="local-small"),
        }.get(agent_id)
    )

    plan = module.prepare_parallel_batch([first, second], assignments, registry=registry)

    assert plan[first.task_id]["memory_profile"] == "rich_synthesis"
    assert plan[second.task_id]["memory_profile"] == "drafting"
    assert first.routing_hints["parallel_family"] == second.routing_hints["parallel_family"]
    assert module.finalize()["parallel_batches_total"] == 1
