from __future__ import annotations

import asyncio

from pydantic import BaseModel

from core.core.kernel_module_manager import KernelModuleManager
from core.core.model_selector import MODEL_DEEPSEEK_R1, ModelSelector
from core.core.models import Complexity, Task, TaskContext, TaskInput, TaskType
from core.core.reasoning_module import ReasoningModule


class _API:
    def __init__(self, modules=None):
        self.modules = modules or {}
        self.logs = []

    def get_module(self, name: str):
        return self.modules.get(name)

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))

    def emit_event(self, event_name: str, payload: dict):
        return None

    def get_context(self, key: str):
        return None

    def query_module_state(self, module_name: str, key: str):
        return None

    def get_memory(self):
        return None


class _AsyncModule:
    name = "async_mod"

    def __init__(self) -> None:
        self.loaded = False
        self.unloaded = False

    async def on_load(self, api) -> None:
        await asyncio.sleep(0)
        self.loaded = True

    async def on_unload(self) -> None:
        await asyncio.sleep(0)
        self.unloaded = True


class _StructuredReply(BaseModel):
    answer: str


class _LocalLLM:
    ready = True

    def query(self, prompt: str, system: str | None = None) -> str:
        return '```json\n{"answer":"ok"}\n```'


def _task(task_type: TaskType = TaskType.REVIEW, complexity: Complexity = Complexity.HIGH) -> Task:
    task = Task(
        task_type,
        TaskInput("review a risky architecture change", files=[]),
        TaskContext("wisper", ".", "main"),
    )
    task.complexity = complexity
    return task


def test_kernel_module_manager_waits_for_async_lifecycle_hooks():
    async def scenario() -> None:
        manager = KernelModuleManager()
        manager.set_api(_API())
        module = _AsyncModule()
        manager.register(module)

        manager.load(module.name)
        assert module.loaded is True
        assert manager.is_loaded(module.name) is True

        manager.unload(module.name)
        assert module.unloaded is True
        assert manager.is_loaded(module.name) is False

    asyncio.run(scenario())


def test_reasoning_local_fallback_strips_markdown_json(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    reasoning = ReasoningModule()
    reasoning.on_load(_API({"local_llm": _LocalLLM()}))

    reply = reasoning.structured_call("return json", _StructuredReply)

    assert reply is not None
    assert reply.answer == "ok"


def test_model_selector_does_not_route_to_openai_without_any_cloud_keys(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    choice = ModelSelector().select(_task())

    assert choice.provider == "local"
    assert choice.model_name == MODEL_DEEPSEEK_R1
