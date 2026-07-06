from __future__ import annotations

from core.core.openai_bazzite_endpoint import discover_openai_endpoint, load_openai_endpoint_discovery, write_openai_endpoint_discovery


def test_discover_openai_endpoint_reads_codex_sale_env(tmp_path):
    home = tmp_path
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "codex-sale.env").write_text(
        "OPENAI_API_KEY=openai_nonsecret_key_value_1234567890\nOPENAI_BASE_URL=https://codex.sale/v1\nCODEX_OPENAI_MODEL=gpt-5.5\n",
        encoding="utf-8",
    )

    discovery = discover_openai_endpoint(home=home, runtime_env={})

    assert discovery.usable is True
    assert discovery.source == "codex-sale.env"
    assert discovery.base_url == "https://codex.sale/v1"
    assert discovery.default_model == "gpt-5.5"
    assert discovery.endpoint_manifest["models"] == "https://codex.sale/v1/models"


def test_discover_openai_endpoint_uses_config_toml_when_env_missing(tmp_path):
    home = tmp_path
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '[openai]\napi_key = "openai_nonsecret_key_value_1234567890"\nbase_url = "https://codex.sale/v1"\ndefault_model = "gpt-5.5"\n',
        encoding="utf-8",
    )

    discovery = discover_openai_endpoint(home=home, runtime_env={})

    assert discovery.usable is True
    assert discovery.base_url == "https://codex.sale/v1"
    assert discovery.default_model == "gpt-5.5"


def test_write_and_load_openai_endpoint_discovery(tmp_path):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "codex-sale.env").write_text(
        "OPENAI_API_KEY=openai_nonsecret_key_value_1234567890\nOPENAI_BASE_URL=https://api.openai.com/v1\n",
        encoding="utf-8",
    )
    output = tmp_path / "reports" / "openai_endpoint_discovery.json"

    payload = write_openai_endpoint_discovery(output_path=output, home=home, runtime_env={})
    loaded = load_openai_endpoint_discovery(output)

    assert payload["usable"] is True
    assert loaded["base_url"] == "https://api.openai.com/v1"
    assert loaded["api_key_present"] is True
    assert loaded["api_key_preview"].startswith("open")
