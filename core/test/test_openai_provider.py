from __future__ import annotations

import pytest

from core.core.openai_provider import default_openai_tcp_probe_hosts, openai_endpoint_manifest, resolve_openai_provider_config, resolve_openai_provider_identity

@pytest.fixture(autouse=True)
def _isolate_codex_user_config(tmp_path, monkeypatch):
    codex_dir = tmp_path / 'empty_codex'
    codex_dir.mkdir()
    (codex_dir / 'config.toml').write_text('', encoding='utf-8')
    missing = tmp_path / 'missing_openai_endpoint_discovery.json'
    missing_inventory = tmp_path / 'missing_openai_runtime_inventory.json'
    missing_generated = tmp_path / 'missing_generated_openai'
    monkeypatch.setenv('AI_BRIDGE_CODEX_CONFIG_DIR', str(codex_dir))
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.setattr('core.core.codex_user_config.candidate_codex_dirs', lambda: [codex_dir])
    monkeypatch.setattr('core.core.openai_bazzite_endpoint.candidate_codex_dirs', lambda: [codex_dir])
    monkeypatch.setattr('core.core.provider_credentials.sync_provider_env_aliases', lambda env=None, override=False: env)
    monkeypatch.setattr('core.core.openai_provider.sync_provider_env_aliases', lambda env=None, override=False: env)
    monkeypatch.setenv('OPENAI_ENDPOINT_DISCOVERY_PATH', str(missing))
    monkeypatch.setenv('OPENAI_RUNTIME_INVENTORY_PATH', str(missing_inventory))
    monkeypatch.setenv('OPENAI_GENERATED_PROFILE_DIR', str(missing_generated))
    monkeypatch.setenv('OPENAI_MODEL_TEMPLATE_MANIFEST_PATH', str(tmp_path / 'missing_model_template_manifest.json'))
    monkeypatch.setenv('OPENAI_ORCHESTRATOR_TEMPLATES_PATH', str(tmp_path / 'missing_orchestrator_templates.json'))
    monkeypatch.setenv('OPENAI_MODELS_FULL_CACHE_PATH', str(tmp_path / 'missing_openai_models_full.json'))
    for key in (
        'OPENAI_API_KEY',
        'CODEX_SALE_API_KEY',
        'CODEX_LB_API_KEY',
        'OPENAI_BASE_URL',
        'AI_BRIDGE_OPENAI_BASE_URL',
        'CODEX_SALE_BASE_URL',
        'AI_BRIDGE_OPENAI_PROVIDER_ID',
        'CODEX_PROVIDER',
        'AI_BRIDGE_CODEX_PROVIDER',
        'CODEX_OPENAI_MODEL',
        'OPENAI_DEFAULT_MODEL',
        'OPENAI_LOW_MODELS',
        'OPENAI_MEDIUM_MODELS',
        'OPENAI_HIGH_MODELS',
        'OPENAI_CRITICAL_MODELS',
        'OPENAI_EXTRA_MODELS',
    ):
        monkeypatch.delenv(key, raising=False)


def test_openai_provider_resolves_codex_sale_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai_nonsecret_key_value_1234567890")
    monkeypatch.setenv("CODEX_SALE_API_KEY", "openai_nonsecret_key_value_1234567890")
    monkeypatch.setenv("CODEX_SALE_BASE_URL", "https://codex.sale")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_MODELS_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_MESSAGES_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_MESSAGES_COUNT_TOKENS_ENDPOINT", raising=False)

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
    monkeypatch.delenv("AI_BRIDGE_OPENAI_MODELS_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_MESSAGES_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_MESSAGES_COUNT_TOKENS_ENDPOINT", raising=False)

    manifest = openai_endpoint_manifest()

    assert manifest["endpoints"]["models"] == "https://codex.sale/v1/models"
    assert manifest["endpoints"]["messages"] == "https://codex.sale/v1/messages"
    assert manifest["endpoints"]["messages_count_tokens"] == "https://codex.sale/v1/messages/count_tokens"
    assert manifest["endpoints"]["codex"] == "https://codex.sale/backend-api/codex"


def test_default_openai_tcp_probe_hosts_uses_custom_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.sale/v1")

    assert default_openai_tcp_probe_hosts() == "codex.sale:443"


def test_openai_provider_bypasses_dead_local_proxy_by_default(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai_nonsecret_key_value_1234567890")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.containers.internal:8012/v1")
    monkeypatch.delenv("AI_BRIDGE_OPENAI_ALLOW_LOCAL_PROXY", raising=False)

    cfg = resolve_openai_provider_config()

    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.models_endpoint == "https://api.openai.com/v1/models"


def test_openai_provider_uses_discovery_artifact_when_env_missing(tmp_path, monkeypatch) -> None:
    discovery = tmp_path / "openai_endpoint_discovery.json"
    discovery.write_text('{"api_key":"openai_nonsecret_key_value_1234567890","base_url":"https://codex.sale/v1","default_model":"gpt-5.5","usable":true}', encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", " ")
    monkeypatch.setenv("CODEX_SALE_API_KEY", " ")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_ENDPOINT_DISCOVERY_PATH", str(discovery))
    monkeypatch.setattr(
        "core.core.openai_provider.load_openai_endpoint_discovery",
        lambda: {
            "api_key": "openai_nonsecret_key_value_1234567890",
            "base_url": "https://codex.sale/v1",
            "default_model": "gpt-5.5",
            "usable": True,
        },
    )
    monkeypatch.setattr("core.core.openai_provider._first_env", lambda *names: "")

    cfg = resolve_openai_provider_config()

    assert cfg.api_key == "openai_nonsecret_key_value_1234567890"
    assert cfg.base_url == "https://codex.sale/v1"
    assert cfg.models_endpoint == "https://codex.sale/v1/models"


def test_openai_provider_identity_normalizes_codex_sale(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.sale/v1")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_PROVIDER_ID", "codex-sale")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_CODEX_ENDPOINT", "https://codex.sale/backend-api/codex")

    identity = resolve_openai_provider_identity()
    manifest = openai_endpoint_manifest()

    assert identity["provider_id"] == "codexsale"
    assert identity["provider_name"] == "Codex Sale"
    assert manifest["provider_id"] == "codexsale"
    assert manifest["provider_name"] == "Codex Sale"


@pytest.fixture(autouse=True)
def _isolate_codex_user_config(tmp_path, monkeypatch):
    codex_dir = tmp_path / 'empty_codex'
    codex_dir.mkdir()
    monkeypatch.setenv('AI_BRIDGE_CODEX_CONFIG_DIR', str(codex_dir))
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.setenv('OPENAI_ENDPOINT_DISCOVERY_PATH', str(tmp_path / 'missing_openai_endpoint_discovery.json'))
