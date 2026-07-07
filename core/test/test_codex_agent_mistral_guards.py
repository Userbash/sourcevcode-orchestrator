from __future__ import annotations

from core.agents.codex_agent import CodexAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType
from core.core.openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR


def _task(description: str) -> Task:
    return Task(
        TaskType.CODE,
        TaskInput(description, files=[]),
        TaskContext("repo", ".", "main"),
    )


def test_codex_agent_mistral_rejects_scaffold_only_prompt_before_network(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")

    class _UnexpectedClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("network client should not be created")

    monkeypatch.setattr("core.agents.codex_agent.httpx.Client", _UnexpectedClient)
    agent = CodexAgent("codex-main")
    agent.set_identity(provider="mistral", model_name="codestral-latest")

    result = agent._run_mistral(_task("placeholder"), "OBJECTIVE:\nFILES:\nACCEPTANCE CRITERIA:")

    assert result.status.value == "failed"
    assert result.errors == [EMPTY_PROVIDER_REQUEST_ERROR]


def test_codex_agent_mistral_rejects_empty_assistant_response(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": ""}}]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, *args, **kwargs) -> _Response:
            return _Response()

    monkeypatch.setattr("core.agents.codex_agent.httpx.Client", _Client)
    agent = CodexAgent("codex-main")
    agent.set_identity(provider="mistral", model_name="codestral-latest")

    result = agent.run(_task("Write patch"))

    assert result.status.value == "failed"
    assert result.errors == [EMPTY_ASSISTANT_RESPONSE_ERROR]
