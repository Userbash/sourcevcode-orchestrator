
from core.agents.mimo_agent import MimoAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType


def _task(task_type: TaskType, description: str):
    return Task(task_type, TaskInput(description), TaskContext("demo", ".", "main"))


def test_mimo_agent_uses_direct_http_for_native_xiaomi_models(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "sk-mimo-test-key")
    monkeypatch.setattr("core.agents.mimo_agent.invoke_mimo_native", lambda model_name, prompt: ({"choices": [{"message": {"content": "ok native"}}]}, None, 200))

    agent = MimoAgent(default_model="xiaomi/mimo-v2.5-pro")
    result = agent.run(_task(TaskType.CODE, "implement api client"))

    assert result.status.value == "done"
    assert result.output["transport"] == "direct_http"
    assert result.model_name == "xiaomi/mimo-v2.5-pro"


def test_mimo_agent_reports_preflight_error_for_mimo_auto_native_http(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "sk-mimo-test-key")

    agent = MimoAgent(default_model="mimo/mimo-auto")
    result = agent.run(_task(TaskType.CODE, "implement api client"))

    assert result.status.value == "failed"
    assert "mimo-auto is not a direct Xiaomi API model" in result.errors[0]
