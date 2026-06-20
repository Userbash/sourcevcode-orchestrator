from core.agents.local_llm_agent import LocalLLMAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType


class _LocalModule:
    ready = True
    model_name = 'qwen2.5:32b-instruct-q4_k_m'
    last_query_metrics = {'latency_ms': 1}

    def __init__(self) -> None:
        self.calls = []

    def query(self, prompt, model_name=None, system=None):
        self.calls.append({'prompt': prompt, 'model_name': model_name, 'system': system})
        return 'ok'


class _Orchestrator:
    def __init__(self, module):
        self.module = module

    def get_module(self, name):
        if name == 'local_llm':
            return self.module
        return None


def _task():
    return Task(TaskType.DOCS, TaskInput('update docs'), TaskContext('demo', '.', 'main'))


def test_local_llm_agent_prefers_assigned_model_override():
    module = _LocalModule()
    agent = LocalLLMAgent('local-1')
    agent.orchestrator = _Orchestrator(module)
    task = _task()
    task.assigned_model = 'qwen-2.5-7b-instruct'

    result = agent.run(task)

    assert result.status.value == 'done'
    assert module.calls[0]['model_name'] == 'qwen-2.5-7b-instruct'
    assert result.model_name == 'qwen-2.5-7b-instruct'
