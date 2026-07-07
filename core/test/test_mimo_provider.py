from unittest.mock import MagicMock

from core.core.mimo_provider import extract_mimo_response_text, invoke_mimo_native, mimo_key_kind, normalize_mimo_model_name, preflight_mimo_native_request, resolve_mimo_provider_config
from core.core.openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR


def test_mimo_key_kind_detects_payg_and_token_plan():
    assert mimo_key_kind("sk-123") == "payg"
    assert mimo_key_kind("tp-123") == "token_plan"


def test_normalize_mimo_model_name_strips_provider_prefix():
    assert normalize_mimo_model_name("xiaomi/mimo-v2.5-pro") == "mimo-v2.5-pro"
    assert normalize_mimo_model_name("mimo/mimo-auto") == "mimo-auto"


def test_preflight_requires_explicit_base_url_for_token_plan(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "tp-123")
    monkeypatch.delenv("MIMO_BASE_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_MIMO_BASE_URL", raising=False)
    cfg = resolve_mimo_provider_config()

    assert preflight_mimo_native_request("xiaomi/mimo-v2.5-pro", cfg) == "Token Plan key detected (tp-...), but MIMO_BASE_URL/AI_BRIDGE_MIMO_BASE_URL is not configured"


def test_invoke_mimo_native_rejects_empty_prompt(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "sk-123")
    monkeypatch.setenv("MIMO_BASE_URL", "https://api.example.test/v1")

    def fail_post(*_args, **_kwargs):
        raise AssertionError("network should not be called")

    monkeypatch.setattr("core.core.mimo_provider.httpx.post", fail_post)

    payload, error_text, status_code = invoke_mimo_native("xiaomi/mimo-v2.5-pro", "OBJECTIVE:   ")

    assert payload is None
    assert error_text == EMPTY_PROVIDER_REQUEST_ERROR
    assert status_code is None


def test_invoke_mimo_native_flags_empty_assistant_response(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "sk-123")
    monkeypatch.setenv("MIMO_BASE_URL", "https://api.example.test/v1")

    response = MagicMock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": "   "}}]}

    monkeypatch.setattr("core.core.mimo_provider.httpx.post", lambda *_args, **_kwargs: response)

    payload, error_text, status_code = invoke_mimo_native("xiaomi/mimo-v2.5-pro", "Write summary")

    assert payload == {"choices": [{"message": {"content": "   "}}]}
    assert error_text == EMPTY_ASSISTANT_RESPONSE_ERROR
    assert status_code == 200
    assert extract_mimo_response_text(payload) == ""
