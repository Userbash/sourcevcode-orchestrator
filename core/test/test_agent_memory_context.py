from __future__ import annotations

from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task, TaskContext, TaskInput, TaskStatus, TaskType
from core.core.orchestrator import Orchestrator
from core.core.session_memory import MemoryScope
from core.core.sourcecraft_module import SourceCraftModule
from core.core.local_llm_module import LocalLLMModule
from core.core.model_selector import ModelChoice
from core.core.models import Complexity
from core.mimo.proxy import MimoOrchestrationDirector
from core.core.prompt_optimizer_module import PromptOptimizerModule
from core.core.task_router import TaskRouter


class MemoryEchoAgent(BaseAgent):
    def __init__(self, agent_id: str = "memory-echo") -> None:
        super().__init__(agent_id, ["code"])
        self.last_context: dict | None = None

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        self.last_context = memory_context or {}
        return self.result(task, f"context_keys={sorted((memory_context or {}).keys())}", TaskStatus.DONE)


def _patch_orchestrator_externals(monkeypatch):
    monkeypatch.setattr(SourceCraftModule, "on_load", lambda self, api: None)
    monkeypatch.setattr(LocalLLMModule, "build_decomposition_draft", lambda self, task, context: {"ready": False, "should_delegate": False, "task_family": "code"})
    monkeypatch.setattr(Orchestrator, "_provider_health_snapshot", lambda self: {})
    monkeypatch.setattr(MimoOrchestrationDirector, "build_selection_context", lambda self, *args, **kwargs: {})
    monkeypatch.setattr(MimoOrchestrationDirector, "validate_and_correct", lambda self, choice, *args, **kwargs: choice)
    monkeypatch.setattr(PromptOptimizerModule, "_antigravity_rewrite", lambda self, task, instruction: None)
    monkeypatch.setattr(TaskRouter, "estimate_complexity", lambda self, task: "low")
    monkeypatch.setattr(Orchestrator, "_select_model_choice_with_mimo", lambda self, task, advisory_context, current_budget, memory_context=None: (ModelChoice("local-small", "local", Complexity.LOW, reason="test_stub"), None))


def _task() -> Task:
    return Task(
        type=TaskType.CODE,
        input=TaskInput(description="x"),
        context=TaskContext(project="p", repo_path=".", branch="main"),
    )


def test_orchestrator_passes_cached_memory_context_to_agent(monkeypatch):
    _patch_orchestrator_externals(monkeypatch)
    orchestrator = Orchestrator()
    agent = MemoryEchoAgent("memory-echo")
    orchestrator.attach_local_agent("memory-echo", agent, agent_type="codex")

    task = _task()
    task.required_capability = "code"
    task.memory_scope = "session"
    task.session_id = "sess-123"
    task.memory_keys = ["project_tree"]

    orchestrator.session_memory.set(MemoryScope.SESSION, "sess-123", "project_tree", {"files": ["a.ts"]})

    result = orchestrator.run_task(task)

    assert result.status == TaskStatus.DONE
    assert (agent.last_context or {}).get("project_tree") == {"files": ["a.ts"]}
    assert "trained_memory_disabled_for_risk" in (agent.last_context or {})


def test_orchestrator_writes_last_result_to_memory(monkeypatch):
    _patch_orchestrator_externals(monkeypatch)
    orchestrator = Orchestrator()
    agent = MemoryEchoAgent("memory-echo")
    orchestrator.attach_local_agent("memory-echo", agent, agent_type="codex")

    task = _task()
    task.required_capability = "code"
    task.memory_scope = "task"

    result = orchestrator.run_task(task)
    assert result.status == TaskStatus.DONE

    stored = orchestrator.session_memory.get(MemoryScope.TASK, task.task_id, "last_result")
    assert stored is not None
    assert stored["task_id"] == task.task_id


def test_orchestrator_passes_reusable_memory_context_for_similar_followup_task(monkeypatch, tmp_path):
    _patch_orchestrator_externals(monkeypatch)
    monkeypatch.setenv("AI_BRIDGE_MEMORY_STORE_DIR", str(tmp_path / "memory_store"))
    orchestrator = Orchestrator()
    agent = MemoryEchoAgent("memory-echo")
    orchestrator.attach_local_agent("memory-echo", agent, agent_type="codex")

    first = Task(
        TaskType.CODE,
        TaskInput("Refactor login parser for oauth callback handling", files=["auth.py"], constraints=["preserve behavior"]),
        TaskContext("p", ".", "main"),
        session_id="sess-reuse",
    )
    first.required_capability = "code"
    first.memory_scope = "session"

    first_result = orchestrator.run_task(first)
    assert first_result.status == TaskStatus.DONE

    second = Task(
        TaskType.CODE,
        TaskInput("Refactor login parser for oauth callback edge cases", files=["auth.py"], constraints=["preserve behavior"]),
        TaskContext("p", ".", "main"),
        session_id="sess-reuse",
    )
    second.required_capability = "code"
    second.memory_scope = "session"

    second_result = orchestrator.run_task(second)
    assert second_result.status == TaskStatus.DONE
    assert "reusable_task_memory_brief" in (agent.last_context or {})
    assert float(agent.last_context.get("reusable_task_memory_similarity", 0.0)) >= 0.45
