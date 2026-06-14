from unittest.mock import MagicMock

from core.agents.mistral_agent import MistralAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType


class _Security:
    def safe_context_for_external_ai(self, payload):
        return payload


def _task(task_type: TaskType, description: str):
    return Task(task_type, TaskInput(description), TaskContext("demo", ".", "main"))


def test_mistral_agent_routes_code_to_codestral(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["model"] = json["model"]
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        response.raise_for_status.return_value = None
        return response

    monkeypatch.setattr("core.agents.mistral_agent.httpx.post", fake_post)
    agent = MistralAgent("mistral-1", _Security())

    result = agent.run(_task(TaskType.CODE, "implement backend api client"))

    assert result.status.value == "done"
    assert captured["model"] == "codestral-latest"


def test_mistral_agent_routes_review_to_large(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["model"] = json["model"]
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        response.raise_for_status.return_value = None
        return response

    monkeypatch.setattr("core.agents.mistral_agent.httpx.post", fake_post)
    agent = MistralAgent("mistral-1", _Security())

    result = agent.run(_task(TaskType.REVIEW, "security review for auth changes"))

    assert result.status.value == "done"
    assert captured["model"] == "mistral-large-latest"
