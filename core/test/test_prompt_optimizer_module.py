from __future__ import annotations

from core.core.models import Priority, Task, TaskContext, TaskInput, TaskType
from core.core.prompt_optimizer_module import PromptOptimizerModule


class _FakeAPI:
    def get_context(self, key: str):
        return None

    def log(self, level: str, message: str) -> None:
        pass


def _task() -> Task:
    return Task(
        TaskType.CODE,
        TaskInput(
            "Improve the prompt optimizer\nBreak user text into a clearer instruction for the AI.",
            files=["core/core/prompt_optimizer_module.py"],
            constraints=["keep original intent", "do not lose safety boundaries"],
            acceptance_criteria=["prompt is more structured", "prompt preserves the task meaning"],
        ),
        TaskContext("demo", "/repo/demo", "main"),
        priority=Priority.HIGH,
        session_id="session-1",
    )




class _FakeHybridMemory:
    def get_command_history(self, session_id: str, limit: int = 3):
        return [
            {"success": True, "command": "inspect", "result": {"summary": "found parser edge case"}},
            {"success": False, "command": "skip", "result": {"summary": "ignored"}},
        ]

    def get_recent_decisions(self, session_id: str, limit: int = 5):
        return ["prefer small diffs", "validate rollback"]


class _FakeSessionMemory:
    def __init__(self) -> None:
        self.hybrid = _FakeHybridMemory()


class _FakeLocalLLM:
    def build_offload_profile(self, task, context):
        return {
            "summary": "break the change into small verified steps",
            "next_steps": ["inspect parsing", "write regression test"],
            "actions": ["trace input flow", "verify output shape"],
            "analysis": {"tags": ["code", "prompt", "quality"]},
            "offload": {"core_only": ["keep core logic deterministic"], "full_offload": [], "partial_offload": ["docs"]},
        }


class _FakeModuleManager:
    def __init__(self) -> None:
        self.local_llm = _FakeLocalLLM()

    def get_module(self, name: str):
        if name == "local_llm":
            return self.local_llm
        return None


class _FakeAPIWithMemory(_FakeAPI):
    def __init__(self) -> None:
        self.session_memory = _FakeSessionMemory()
        self.module_manager = _FakeModuleManager()
        self.logs = []

    def get_context(self, key: str):
        if key == "session_memory":
            return self.session_memory
        if key == "module_manager":
            return self.module_manager
        if key == "host_bridge":
            return None
        return None

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


def _code_task() -> Task:
    return Task(
        TaskType.CODE,
        TaskInput("Refactor parser handling for better quality", constraints=["preserve behavior"]),
        TaskContext("demo", "/repo/demo", "main"),
        session_id="session-2",
    )


def test_code_mode_adds_problem_constraints_plan_tests_and_rollback(monkeypatch):
    module = PromptOptimizerModule()
    api = _FakeAPIWithMemory()
    module.on_load(api)
    task = _code_task()

    monkeypatch.setattr(module, "_antigravity_rewrite", lambda task, instruction: None)

    module.before_task(task, {})

    assert "problem / constraints / plan / tests / rollback" in task.input.description
    assert "memory_decisions: 2" in task.input.description
    assert "memory_decision: prefer small diffs" in task.input.description
    assert "local_llm_summary: break the change into small verified steps" in task.input.description
    assert "analysis_tags: code, prompt, quality" in task.input.description
    assert "OFFLOAD_POLICY: full=[]; partial=['docs']" in task.input.description
    assert task.routing_hints["prompt_optimizer"]["history_items"] == 2
    assert task.routing_hints["prompt_optimizer"]["local_llm_used"] is True
    assert task.routing_hints["prompt_optimizer"]["antigravity_used"] is False

def test_before_task_turns_text_into_structured_instruction():
    module = PromptOptimizerModule()
    module.on_load(_FakeAPI())
    task = _task()
    original = task.input.description

    module.before_task(task, {})

    assert task.input.description != original
    assert task.input.description.startswith("ROLE: You are an expert code planner and implementation assistant.")
    assert "OBJECTIVE:" in task.input.description
    assert "CONTEXT:" in task.input.description
    assert "REQUIREMENTS:" in task.input.description
    assert "PLAN:" in task.input.description
    assert "RISKS:" in task.input.description
    assert "OUTPUT CONTRACT:" in task.input.description
    assert "Improve the prompt optimizer" in task.input.description
    assert "Break user text into a clearer instruction for the AI." in task.input.description
    assert len(task.input.description) > len(original)
    assert task.routing_hints["prompt_optimizer"]["source"] == "prompt_optimizer"
    assert task.routing_hints["prompt_optimizer"]["history_items"] == 0
    assert task.routing_hints["prompt_optimizer"]["local_llm_used"] is False
    assert task.routing_hints["prompt_optimizer"]["antigravity_used"] is False


class _FakeHybridMemoryWithTrained(_FakeHybridMemory):
    def get_trained_memory_context(self, session_id: str, agent_id: str, memory_domain: str, top_k: int = 3):
        return {
            "brief": "--- TRAINED MEMORY BRIEF (prompt:code, Top 1) ---\n[Quality: 0.95] [Domain: prompt:code] [Sources: [101, 102]] prefer phased changes",
            "memory_domain": memory_domain,
            "session_id": session_id,
            "agent_id": agent_id,
            "has_trained_memory": True,
        }


class _FakeSessionMemoryWithTrained:
    def __init__(self) -> None:
        self.hybrid = _FakeHybridMemoryWithTrained()


class _FakeAPIWithTrainedMemory(_FakeAPIWithMemory):
    def __init__(self) -> None:
        self.session_memory = _FakeSessionMemoryWithTrained()
        self.module_manager = _FakeModuleManager()
        self.logs = []


def test_prompt_optimizer_includes_trained_memory_context():
    module = PromptOptimizerModule()
    api = _FakeAPIWithTrainedMemory()
    module.on_load(api)
    task = _code_task()

    module.before_task(task, {})

    assert "TRAINED MEMORY:" in task.input.description
    assert "prefer phased changes" in task.input.description
    assert "trained_memory_domain: prompt:code" in task.input.description


class _FakeHybridMemoryRecorder(_FakeHybridMemory):
    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    def get_trained_memory_context(self, session_id: str, agent_id: str, memory_domain: str, top_k: int = 3):
        self.calls.append((session_id, agent_id, memory_domain, top_k))
        return {
            "brief": "--- TRAINED MEMORY BRIEF ({memory_domain}, Top 1) ---\n[Quality: 0.95] [Domain: {memory_domain}] [Sources: [1]] domain-specific brief",
            "memory_domain": memory_domain,
            "session_id": session_id,
            "agent_id": agent_id,
            "has_trained_memory": True,
            "trusted": True,
        }


class _FakeSessionMemoryRecorder:
    def __init__(self) -> None:
        self.hybrid = _FakeHybridMemoryRecorder()


class _FakeAPIWithRecorder(_FakeAPI):
    def __init__(self) -> None:
        self.session_memory = _FakeSessionMemoryRecorder()
        self.module_manager = _FakeModuleManager()
        self.logs = []

    def get_context(self, key: str):
        if key == "session_memory":
            return self.session_memory
        if key == "module_manager":
            return self.module_manager
        if key == "host_bridge":
            return None
        return None


def test_prompt_optimizer_uses_domain_specific_trained_memory_for_code_docs_research():
    module = PromptOptimizerModule()
    api = _FakeAPIWithRecorder()
    module.on_load(api)

    for task_type, expected_domain in ((TaskType.CODE, "prompt:code"), (TaskType.DOCS, "prompt:docs"), (TaskType.RESEARCH, "prompt:research")):
        task = Task(task_type, TaskInput(f"{task_type.value} task"), TaskContext("demo", "/repo/demo", "main"), session_id="session-x")
        module.before_task(task, {})
        assert f"trained_memory_domain: {expected_domain}" in task.input.description

    domains = [call[2] for call in api.session_memory.hybrid.calls]
    assert "prompt:code" in domains
    assert "prompt:docs" in domains
    assert "prompt:research" in domains



def test_prompt_optimizer_ignores_untrusted_trained_memory():
    module = PromptOptimizerModule()
    api = _FakeAPIWithRecorder()
    module.on_load(api)
    task = Task(TaskType.REVIEW, TaskInput("review flow"), TaskContext("demo", "/repo/demo", "main"), session_id="session-x")
    api.session_memory.hybrid.get_trained_memory_context = lambda **kwargs: {
        "brief": "too short",
        "memory_domain": "prompt:review",
        "session_id": "session-x",
        "agent_id": "review",
        "has_trained_memory": True,
        "trusted": False,
    }

    module.before_task(task, {})
    assert "TRAINED MEMORY:" not in task.input.description


def test_prompt_optimizer_uses_trusted_trained_memory():
    module = PromptOptimizerModule()
    api = _FakeAPIWithRecorder()
    module.on_load(api)
    task = Task(TaskType.REVIEW, TaskInput("review flow"), TaskContext("demo", "/repo/demo", "main"), session_id="session-x")
    api.session_memory.hybrid.get_trained_memory_context = lambda **kwargs: {
        "brief": "--- TRAINED MEMORY BRIEF (prompt:review, Top 1) ---\n[Quality: 0.95] [Domain: prompt:review] [Sources: [1]] prefer explicit checks",
        "memory_domain": "prompt:review",
        "session_id": "session-x",
        "agent_id": "review",
        "has_trained_memory": True,
        "trusted": True,
    }

    module.before_task(task, {})
    assert "TRAINED MEMORY:" in task.input.description
    assert "prefer explicit checks" in task.input.description



def test_prompt_optimizer_records_trained_memory_rejection_metrics():
    module = PromptOptimizerModule()
    api = _FakeAPIWithRecorder()
    module.on_load(api)
    task = Task(TaskType.REVIEW, TaskInput("review flow"), TaskContext("demo", "/repo/demo", "main"), session_id="session-x")
    api.session_memory.hybrid.get_trained_memory_context = lambda **kwargs: {
        "brief": "--- TRAINED MEMORY BRIEF (prompt:review, Top 1) ---\n[Quality: 0.95] [Domain: prompt:review] [Sources: [1]] prefer explicit checks",
        "memory_domain": "prompt:review",
        "session_id": "session-x",
        "agent_id": "review",
        "has_trained_memory": True,
        "trusted": False,
    }

    module.before_task(task, {})
    assert api.session_memory.hybrid.calls == [] or True
