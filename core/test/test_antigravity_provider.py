from unittest.mock import MagicMock

from core.core.antigravity_provider import extract_antigravity_response_text, invoke_antigravity_native
from core.core.openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR


def test_invoke_antigravity_native_rejects_empty_prompt(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "token-123")
    monkeypatch.setenv("ANTIGRAVITY_BASE_URL", "https://api.example.test/v1beta/openai")

    def fail_post(*_args, **_kwargs):
        raise AssertionError("network should not be called")

    monkeypatch.setattr("core.core.antigravity_provider.httpx.post", fail_post)

    payload, error_text, status_code = invoke_antigravity_native("gemini-2.5-flash-lite", "OBJECTIVE:   ")

    assert payload is None
    assert error_text == EMPTY_PROVIDER_REQUEST_ERROR
    assert status_code is None


def test_invoke_antigravity_native_flags_empty_assistant_response(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "token-123")
    monkeypatch.setenv("ANTIGRAVITY_BASE_URL", "https://api.example.test/v1beta/openai")

    response = MagicMock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": []}}]}

    monkeypatch.setattr("core.core.antigravity_provider.httpx.post", lambda *_args, **_kwargs: response)

    payload, error_text, status_code = invoke_antigravity_native("gemini-2.5-flash-lite", "Explain repo")

    assert payload == {"choices": [{"message": {"content": []}}]}
    assert error_text == EMPTY_ASSISTANT_RESPONSE_ERROR
    assert status_code == 200
    assert extract_antigravity_response_text(payload) == ""
