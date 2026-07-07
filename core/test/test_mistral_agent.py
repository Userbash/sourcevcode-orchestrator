from unittest.mock import MagicMock

from core.agents.mistral_agent import MistralAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType
from core.core.openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR


class _Security:
    def safe_context_for_external_ai(self, payload):
        return payload


def _task(task_type: TaskType, description: str):
    return Task(task_type, TaskInput(description), TaskContext("demo", ".", "main"))


def test_mistral_agent_routes_code_to_codestral(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")
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
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")
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



def test_mistral_agent_prefers_assigned_model_override(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["model"] = json["model"]
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        response.raise_for_status.return_value = None
        return response

    monkeypatch.setattr("core.agents.mistral_agent.httpx.post", fake_post)
    agent = MistralAgent("mistral-1", _Security())
    task = _task(TaskType.CODE, "implement backend api client")
    task.assigned_model = "mistral-medium-latest"

    result = agent.run(task)

    assert result.status.value == "done"
    assert captured["model"] == "mistral-medium-latest"
    assert result.model_name == "mistral-medium-latest"



def test_mistral_agent_falls_back_to_fast_model_after_transient_failure(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")
    monkeypatch.setenv("MISTRAL_MODEL", "mistral-large-latest")
    monkeypatch.setenv("MISTRAL_FAST_MODEL", "mistral-medium-latest")
    seen: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        model = json["model"]
        seen.append(model)
        response = MagicMock()
        if model == "mistral-large-latest":
            response.status_code = 503
            response.raise_for_status.side_effect = RuntimeError("service busy")
            return response
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        response.raise_for_status.return_value = None
        return response

    monkeypatch.setattr("core.agents.mistral_agent.httpx.post", fake_post)
    monkeypatch.setattr("core.agents.mistral_agent.time.sleep", lambda *_args, **_kwargs: None)
    agent = MistralAgent("mistral-1", _Security())

    result = agent.run(_task(TaskType.RESEARCH, "collect runtime readiness summary"))

    assert result.status.value == "done"
    assert seen[-1] == "mistral-medium-latest"
    assert result.model_name == "mistral-medium-latest"


def test_mistral_agent_rejects_empty_request_before_network(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")

    def fail_post(*_args, **_kwargs):
        raise AssertionError("network should not be called")

    monkeypatch.setattr("core.agents.mistral_agent.httpx.post", fail_post)
    agent = MistralAgent("mistral-1", _Security())

    result = agent.run(_task(TaskType.CODE, ""))

    assert result.status.value == "failed"
    assert result.errors == [EMPTY_PROVIDER_REQUEST_ERROR]


def test_mistral_agent_rejects_empty_assistant_response(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")

    def fake_post(url, headers=None, json=None, timeout=None):
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": [{"type": "text", "text": "   "}]}}]}
        response.raise_for_status.return_value = None
        return response

    monkeypatch.setattr("core.agents.mistral_agent.httpx.post", fake_post)
    agent = MistralAgent("mistral-1", _Security())

    result = agent.run(_task(TaskType.REVIEW, "security review"))

    assert result.status.value == "failed"
    assert result.errors == [EMPTY_ASSISTANT_RESPONSE_ERROR]
