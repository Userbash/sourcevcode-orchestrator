from core.core.mimo_provider import mimo_key_kind, normalize_mimo_model_name, preflight_mimo_native_request, resolve_mimo_provider_config


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
