from __future__ import annotations

from core.core.openai_provider import default_openai_tcp_probe_hosts, resolve_openai_provider_config


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


def test_default_openai_tcp_probe_hosts_uses_custom_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.sale/v1")

    assert default_openai_tcp_probe_hosts() == "codex.sale:443"
