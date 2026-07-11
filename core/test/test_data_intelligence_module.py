import asyncio
from dataclasses import dataclass, field

from core.core.data_intelligence_module import DataIntelligenceModule


@dataclass
class FakeInput:
    description: str


@dataclass
class FakeTask:
    id: str
    session_id: str
    input: FakeInput
    routing_hints: dict = field(default_factory=dict)


class FakeRecord:
    def __init__(self, content):
        self.content = content


class FakePersistentMemory:
    def __init__(self):
        self.stored = []
        self.records = [
            FakeRecord({
                "keywords": ["analytics", "pipeline", "agent"],
                "phrases": ["analytics pipeline"],
                "templates": [{"type": "key_value", "key": "mode", "value": "fast"}],
            })
        ]

    def store_memory(self, **kwargs):
        self.stored.append(kwargs)
        return len(self.stored)

    def retrieve_memories(self, **kwargs):
        return list(self.records)


class FakeHybridMemory:
    def __init__(self):
        self.persistent = FakePersistentMemory()
        self.values = {}

    def set_by_full_key(self, key, value, **kwargs):
        self.values[key] = {"value": value, "meta": kwargs}


class FakeSessionMemory:
    def __init__(self):
        self.hybrid = FakeHybridMemory()


class FakeLocalLLM:
    def generate_embedding_keywords(self, text):
        return ["data science", "knowledge graph", "prompt enrichment"]


class FakeModuleManager:
    def get_module(self, name):
        if name == "local_llm":
            return FakeLocalLLM()
        raise KeyError(name)


class FakeHub:
    def __init__(self):
        self.events = []

    def publish_agent_event(self, payload):
        self.events.append(payload)


class FakeAPI:
    def __init__(self):
        self.runtime_event_stream_hub = FakeHub()
        self.context = {
            "session_memory": FakeSessionMemory(),
            "module_manager": FakeModuleManager(),
        }

    def get_context(self, name):
        return self.context.get(name)


def test_before_task_builds_prompt_pool_and_persists_artifacts():
    module = DataIntelligenceModule()
    api = FakeAPI()
    asyncio.run(module.on_load(api))

    task = FakeTask(
        id="task-1",
        session_id="session-1",
        input=FakeInput(
            description=(
                "Build analytics agent for data science pipeline.\n"
                "mode: accelerated\n"
                "stage | owner | output\n"
                "collect | agent | metrics\n"
                "Connect prompt enrichment and keyword search optimization."
            )
        ),
    )
    context = {}

    module.before_task(task, context)

    assert "data_intelligence" in context
    assert "prompt_data_pool" in context
    assert context["prompt_data_pool"]["keywords"]
    assert any("analytics" in keyword for keyword in context["prompt_data_pool"]["keywords"])
    assert task.routing_hints["data_intelligence"]["generated_text_available"] is True
    assert api.context["session_memory"].hybrid.values
    assert api.context["session_memory"].hybrid.persistent.stored
    assert api.runtime_event_stream_hub.events


def test_before_task_ignores_empty_description():
    module = DataIntelligenceModule()
    api = FakeAPI()
    asyncio.run(module.on_load(api))

    task = FakeTask(id="task-2", session_id="session-2", input=FakeInput(description="   "))
    context = {}

    module.before_task(task, context)

    assert context == {}
    assert task.routing_hints == {}
