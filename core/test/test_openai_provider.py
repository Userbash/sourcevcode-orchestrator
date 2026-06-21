from __future__ import annotations

from core.core.openai_provider import default_openai_tcp_probe_hosts, openai_endpoint_manifest, resolve_openai_provider_config


def test_openai_provider_resolves_codex_sale_base_url(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_SALE_API_KEY", "openai_nonsecret_key_value_1234567890")
    monkeypatch.setenv("CODEX_SALE_BASE_URL", "https://codex.sale")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_BASE_URL", raising=False)

    cfg = resolve_openai_provider_config()

    assert cfg.api_key == "openai_nonsecret_key_value_1234567890"
    assert cfg.base_url == "https://codex.sale/v1"
    assert cfg.models_endpoint == "https://codex.sale/v1/models"
    assert cfg.chat_completions_endpoint == "https://codex.sale/v1/chat/completions"
    assert cfg.responses_endpoint == "https://codex.sale/v1/responses"
    assert cfg.messages_endpoint == "https://codex.sale/v1/messages"
    assert cfg.messages_count_tokens_endpoint == "https://codex.sale/v1/messages/count_tokens"
    assert cfg.codex_endpoint == "https://codex.sale/backend-api/codex"


def test_openai_endpoint_manifest_includes_new_routes(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.sale/v1")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_CODEX_ENDPOINT", "https://codex.sale/backend-api/codex")

    manifest = openai_endpoint_manifest()

    assert manifest["endpoints"]["models"] == "https://codex.sale/v1/models"
    assert manifest["endpoints"]["messages"] == "https://codex.sale/v1/messages"
    assert manifest["endpoints"]["messages_count_tokens"] == "https://codex.sale/v1/messages/count_tokens"
    assert manifest["endpoints"]["codex"] == "https://codex.sale/backend-api/codex"


def test_default_openai_tcp_probe_hosts_uses_custom_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.sale/v1")

    assert default_openai_tcp_probe_hosts() == "codex.sale:443"
