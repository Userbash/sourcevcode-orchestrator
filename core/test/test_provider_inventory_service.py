from __future__ import annotations

import json
import time

import pytest
from pathlib import Path

from core.core.provider_inventory_service import ProviderInventoryService


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

def test_provider_inventory_service_reads_provider_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "provider_inventory_snapshot.json"
    snapshot.write_text(json.dumps({"updated_at": int(time.time()), "providers": {"mistral": {"provider": "mistral", "models": ["mistral-large-latest"], "source": "cache", "ok": True}}}), encoding="utf-8")
    monkeypatch.setenv("PROVIDER_INVENTORY_SNAPSHOT_PATH", str(snapshot))

    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._generated_mimo_models", staticmethod(lambda: []))
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._artifact_mimo_models", staticmethod(lambda: {"usable": [], "ping": []}))
    service = ProviderInventoryService()
    provider = service.provider_snapshot("mistral")

    assert provider["provider"] == "mistral"
    assert provider["models"] == ["mistral-large-latest"]
    assert provider["source"] == "cache"


def test_provider_inventory_service_builds_participation_snapshot(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "model_ping_report.json").write_text(
        '{"openai": {"models": [{"model": "gpt-5.4", "ok": true}, {"model": "gpt-5.5", "ok": true}]}, "mistral": {"models": [{"model": "mistral-large-latest", "ok": true}, {"model": "codestral-embed", "ok": false, "skipped": true, "skip_reason": "embedding_model"}, {"model": "labs-leanstral-2603", "ok": false, "status_code": 403, "error": "Labs model"}]}, "local_llm": {"models": [{"model": "qwen2.5:32b-instruct-q4_k_m", "ok": true}, {"model": "qwen-2.5-7b-instruct", "ok": false, "error": ""}]}, "antigravity": {"models": [{"model": "antigravity-pro", "ok": false, "error": "not found"}]}}',
        encoding='utf-8',
    )
    (report_dir / "mimo_model_ping_report.json").write_text(
        '{"models": [{"model": "github-copilot/gpt-5.2", "ok": false, "error": "Personal Access Tokens are not supported for this endpoint"}]}',
        encoding='utf-8',
    )
    (report_dir / "mimo_usable_models.json").write_text(
        '{"models": [{"model": "mistral/mistral-large-latest", "ok": true}, {"model": "openai/gpt-5.4", "ok": true}]}',
        encoding='utf-8',
    )
    monkeypatch.setenv("PROVIDER_INVENTORY_REPORT_DIR", str(report_dir))

    class _Record:
        def __init__(self, provider, model_name):
            self.provider = provider
            self.model_name = model_name

    service = ProviderInventoryService()
    snap = service.build_participation_snapshot([_Record("mistral", "mistral-large-latest"), _Record("local", "qwen2.5:32b-instruct-q4_k_m"), _Record("antigravity", "antigravity-pro")])

    assert any(item["model_name"] == "mistral-large-latest" and item["source"] == "registered_agent" for item in snap["active_now"])
    assert any(item["model_name"] == "mistral/mistral-large-latest" and item["source"] == "mimo_usable" for item in snap["active_now"])
    assert any(item["model_name"] == "gpt-5.5" for item in snap["available_but_not_wired_directly"])
    assert any(item["model_name"] == "codestral-embed" and item["reason"] == "embedding_model" for item in snap["present_but_unusable"])
    assert any(item["model_name"] == "github-copilot/gpt-5.2" and item["reason"] == "github_pat_not_supported" for item in snap["present_but_unusable"])
    assert any(item["model_name"] == "qwen-2.5-7b-instruct" and item["reason"] == "probe_failed" for item in snap["present_but_unusable"])
    assert any(item["model_name"] == "antigravity-pro" and item["reason"] == "direct_api_missing_or_unready" for item in snap["present_but_unusable"])


def test_provider_inventory_service_syncs_openai_artifacts(monkeypatch):
    calls = {}

    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.5", "claude-sonnet-4-6"]
        def diagnostics(self):
            return {"ok": True, "source": "live"}

    monkeypatch.setattr("core.core.provider_inventory_service.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr("core.core.provider_inventory_service.resolve_openai_provider_config", lambda: type("Cfg", (), {"base_url": "https://codex.sale/v1"})())

    def _fake_sync(models, *, base_url='', **kwargs):
        calls["models"] = list(models)
        calls["base_url"] = base_url
        return {"generated_profile_root": "generated", "orchestrator_templates_path": "templates.json", "model_template_manifest": {"summary": {"routable_count": 0}, "models": []}}

    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", _fake_sync)

    service = ProviderInventoryService()
    entry = service.collect(force_refresh=True)["openai"]

    assert calls["models"] == ["gpt-5.5", "claude-sonnet-4-6"]
    assert calls["base_url"] == "https://codex.sale/v1"
    assert entry["diagnostics"]["artifact_sync"]["orchestrator_templates_path"] == "templates.json"


def test_provider_inventory_service_mimo_uses_direct_catalog(monkeypatch):
    monkeypatch.setattr("core.core.provider_inventory_service.fetch_mimo_model_catalog", lambda force_refresh=False: {"models": ["xiaomi/mimo-v2.5-pro"], "source": "live", "status_code": 200, "error": None})
    monkeypatch.setattr("core.core.provider_inventory_service.sync_mimo_native_artifacts", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr("core.core.provider_inventory_service.configured_native_mimo_models", lambda: ["xiaomi/mimo-v2.5-pro"])
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._generated_mimo_models", staticmethod(lambda: []))
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._artifact_mimo_models", staticmethod(lambda: {"usable": [], "ping": []}))
    service = ProviderInventoryService()
    entry = service._mimo_entry(force_refresh=True)
    assert entry.ok is True
    assert entry.source == "direct_http_catalog"
    assert entry.models == ["xiaomi/mimo-v2.5-pro"]


def test_provider_inventory_service_mimo_falls_back_to_generated_catalog(monkeypatch):
    monkeypatch.setattr("core.core.provider_inventory_service.fetch_mimo_model_catalog", lambda force_refresh=False: {"models": [], "source": "unconfigured", "status_code": None, "error": "MIMO_API_KEY not set"})
    monkeypatch.setattr("core.core.provider_inventory_service.configured_native_mimo_models", lambda: [])
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._generated_mimo_models", staticmethod(lambda: ["mimo-v2-pro", "mimo-v2.5"]))
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._artifact_mimo_models", staticmethod(lambda: {"usable": [], "ping": []}))
    service = ProviderInventoryService()
    entry = service._mimo_entry(force_refresh=True)
    assert entry.ok is True
    assert entry.models == ["xiaomi/mimo-v2-pro", "xiaomi/mimo-v2.5"]



def test_provider_inventory_service_refreshes_mimo_usable_snapshot(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    monkeypatch.setenv("PROVIDER_INVENTORY_REPORT_DIR", str(report_dir))
    monkeypatch.setenv("AI_BRIDGE_MIMO_AUTO_PING_LIMIT", "0")

    class _Bridge:
        def __init__(self):
            self.is_catalog_available = True
        def refresh_cache_sync(self):
            return [
                type("Snap", (), {"full_id": "xiaomi/mimo-v2.5-pro", "id": "mimo-v2.5-pro"})(),
                type("Snap", (), {"full_id": "xiaomi/mimo-v2.5", "id": "mimo-v2.5"})(),
                type("Snap", (), {"full_id": "github-models/gpt-5.4", "id": "gpt-5.4"})(),
            ]

    def _fake_run(cmd, capture_output=True, text=True, timeout=25, check=False):
        model = cmd[4]
        stdout = json.dumps({"type": "text", "part": {"text": f"pong from {model}"}}) + "\n"
        return type("Proc", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()


    monkeypatch.setattr("core.core.provider_inventory_service.configured_native_mimo_models", lambda: ["xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5", "xiaomi/mimo-v2-flash"])

    def _fake_probe(model_name, prompt, *, group=None, timeout_sec=20.0):
        return ({"choices": [{"message": {"content": f"pong from {model_name}"}}]}, None, 200, group or "text")

    monkeypatch.setattr("core.core.provider_inventory_service.invoke_mimo_group_probe", _fake_probe)
    service = ProviderInventoryService()
    result = service.refresh_mimo_usable_snapshot(force_refresh=True)

    assert result["status"] == "ok"
    assert result["probed_count"] == 3
    assert result["text_ready_count"] == 3
    payload = json.loads((report_dir / "mimo_usable_models.json").read_text(encoding="utf-8"))
    assert payload["usable_count"] == 3
    assert payload["text_ready_count"] == 3
    assert payload["models"][0]["model"] == "xiaomi/mimo-v2.5-pro"


def test_provider_inventory_service_normalizes_mimo_aliases():
    assert ProviderInventoryService._normalize_provider("xiaomi") == "mimo"
    assert ProviderInventoryService._normalize_provider("github-models") == "mimo"


def test_provider_inventory_service_mimo_group_probe_helpers_are_group_aware():
    from core.core.mimo_provider import build_mimo_probe_payload, mimo_group_use_case, mimo_model_group, mimo_model_subgroup, mimo_probe_mode_for_group

    assert mimo_model_group("xiaomi/mimo-v2.5-pro") == "text"
    assert mimo_model_group("xiaomi/mimo-v2.5-asr") == "asr"
    assert mimo_model_group("xiaomi/mimo-v2.5-tts") == "tts"
    assert mimo_model_group("xiaomi/mimo-v2-omni") == "multimodal"
    assert mimo_probe_mode_for_group("asr") == "input_audio"
    assert mimo_probe_mode_for_group("tts") == "assistant_text"
    assert mimo_model_subgroup("xiaomi/mimo-v2.5-tts-voiceclone") == "voice_clone"
    assert mimo_model_subgroup("xiaomi/mimo-v2.5-tts-voicedesign") == "voice_design"
    assert mimo_group_use_case("tts").startswith("Speech output")
    assert build_mimo_probe_payload("xiaomi/mimo-v2.5-asr", "reply", group="asr")["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert build_mimo_probe_payload("xiaomi/mimo-v2.5-tts", "reply", group="tts")["messages"][0]["role"] == "assistant"
    assert build_mimo_probe_payload("xiaomi/mimo-v2.5-tts-voiceclone", "reply", group="tts")["audio"]["data"].startswith("data:audio/wav;base64,")
    assert build_mimo_probe_payload("xiaomi/mimo-v2.5-tts-voiceclone", "reply", group="tts")["audio"]["voice"].startswith("data:audio/wav;base64,")
    assert build_mimo_probe_payload("xiaomi/mimo-v2.5-tts-voiceclone", "reply", group="tts")["messages"][0]["role"] == "assistant"
    assert build_mimo_probe_payload("xiaomi/mimo-v2.5-tts-voicedesign", "reply", group="tts")["messages"][0]["role"] == "user"


def test_provider_inventory_service_selects_all_mimo_models_by_default(monkeypatch):
    monkeypatch.delenv("AI_BRIDGE_MIMO_AUTO_PING_LIMIT", raising=False)
    models = ["xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5", "xiaomi/mimo-v2-pro"]

    selected = ProviderInventoryService._select_mimo_probe_models(models)

    assert selected == models


def test_provider_inventory_service_marks_invalid_api_key_as_auth_failed():
    reason, remediation = ProviderInventoryService._row_reason('mimo', {
        'model': 'xiaomi/mimo-v2.5-pro',
        'error': 'Invalid API Key: Please provide valid API Key',
        'status_code': 401,
    })

    assert reason == 'auth_failed'
    assert 'credentials' in remediation


def test_provider_inventory_service_disables_mimo_via_env(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_MIMO_ENABLED', 'false')

    service = ProviderInventoryService()
    entry = service._mimo_entry(force_refresh=True)

    assert entry.source == 'disabled_by_env'
    assert entry.error == 'mimo_disabled_by_env'
    assert entry.diagnostics['enabled'] is False
    assert service.refresh_mimo_usable_snapshot(force_refresh=True)['status'] == 'disabled'


def test_provider_inventory_service_auto_refreshes_stale_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "provider_inventory_snapshot.json"
    snapshot.write_text(json.dumps({
        "updated_at": int(time.time()) - 7200,
        "providers": {"mimo": {"provider": "mimo", "models": ["old-model"], "source": "cache", "ok": True}},
    }), encoding="utf-8")
    monkeypatch.setenv("PROVIDER_INVENTORY_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("AI_BRIDGE_PROVIDER_INVENTORY_REFRESH_INTERVAL_SEC", "60")
    monkeypatch.setattr(ProviderInventoryService, "_testing_mode", staticmethod(lambda: False))

    service = ProviderInventoryService()

    def _fake_refresh(*, force_refresh=False):
        assert force_refresh is True
        return {"mimo": {"provider": "mimo", "models": ["fresh-model"], "source": "bridge_live_sync", "ok": True}}

    service.refresh = _fake_refresh
    provider = service.provider_snapshot("mimo")

    assert provider["models"] == ["fresh-model"]
    assert provider["source"] == "bridge_live_sync"


def test_provider_inventory_service_skips_stale_auto_refresh_in_testing_mode(tmp_path, monkeypatch):
    snapshot = tmp_path / "provider_inventory_snapshot.json"
    snapshot.write_text(json.dumps({
        "updated_at": int(time.time()) - 7200,
        "providers": {"mimo": {"provider": "mimo", "models": ["cached-model"], "source": "cache", "ok": True}},
    }), encoding="utf-8")
    monkeypatch.setenv("PROVIDER_INVENTORY_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("AI_BRIDGE_PROVIDER_INVENTORY_REFRESH_INTERVAL_SEC", "60")
    monkeypatch.setattr(ProviderInventoryService, "_testing_mode", staticmethod(lambda: True))

    service = ProviderInventoryService()
    service.refresh = lambda *, force_refresh=False: (_ for _ in ()).throw(AssertionError("refresh should not run in testing mode"))

    provider = service.provider_snapshot("mimo")

    assert provider["models"] == ["cached-model"]
    assert provider["source"] == "cache"


def test_provider_inventory_service_mimo_merges_artifact_models_into_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr("core.core.provider_inventory_service.fetch_mimo_model_catalog", lambda force_refresh=False: {"models": ["xiaomi/mimo-v2.5-pro"], "source": "cache", "status_code": 200, "error": None})
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "mimo_model_ping_report.json").write_text(json.dumps({"models": [{"model": "xiaomi/mimo-v2.5", "ok": False, "error": "auth"}]}), encoding="utf-8")
    (report_dir / "mimo_usable_models.json").write_text(json.dumps({"models": [{"model": "xiaomi/mimo-v2.5-pro", "ok": True}]}), encoding="utf-8")
    monkeypatch.setenv("PROVIDER_INVENTORY_REPORT_DIR", str(report_dir))
    monkeypatch.setattr("core.core.provider_inventory_service.configured_native_mimo_models", lambda: ["xiaomi/mimo-v2.5-pro"])
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._generated_mimo_models", staticmethod(lambda: []))
    service = ProviderInventoryService()
    entry = service._mimo_entry(force_refresh=False)
    assert entry.models == ["xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5"]
    assert entry.diagnostics["usable_artifact_models_count"] == 1
    assert entry.diagnostics["ping_artifact_models_count"] == 1


def test_provider_inventory_service_reads_mimo_models_from_generated_profiles(tmp_path, monkeypatch):
    generated_root = tmp_path / "generated" / "mimo_native"
    model_dir = generated_root / "models"
    model_dir.mkdir(parents=True)
    (generated_root / "manifest.json").write_text(json.dumps({
        "models": ["gpt-5.5", "mimo-v2.5-pro"],
        "model_profiles": ["models/model__mimo-v2.5-pro.json", "models/model__gpt-5.5.json"],
    }), encoding='utf-8')
    (model_dir / "model__mimo-v2.5-pro.json").write_text(json.dumps({
        "profile_key": "model::mimo-v2.5-pro",
        "metadata": {"model_family": "mimo"},
    }), encoding='utf-8')
    (model_dir / "model__gpt-5.5.json").write_text(json.dumps({
        "profile_key": "model::gpt-5.5",
        "metadata": {"model_family": "gpt"},
    }), encoding='utf-8')
    monkeypatch.setenv("MIMO_GENERATED_PROFILE_DIR", str(generated_root))
    monkeypatch.setenv("MIMO_MODELS_FULL_CACHE_PATH", str(tmp_path / "missing_cache.json"))

    models = ProviderInventoryService._generated_mimo_models()

    assert models == ["mimo-v2.5-pro"]


def test_provider_inventory_service_stale_when_sources_newer_than_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "provider_inventory_snapshot.json"
    snapshot.write_text(json.dumps({"updated_at": 100, "providers": {}}), encoding='utf-8')
    cache = tmp_path / "openai_models_full.json"
    cache.write_text(json.dumps({"models": ["mimo-v2.5-pro"]}), encoding='utf-8')
    monkeypatch.setenv("PROVIDER_INVENTORY_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("OPENAI_MODELS_FULL_CACHE_PATH", str(cache))

    service = ProviderInventoryService()

    assert service._snapshot_is_stale({"updated_at": 100, "providers": {}}) is True


def test_provider_inventory_service_selects_openai_probe_models_with_priority(monkeypatch):
    monkeypatch.setenv("CODEX_OPENAI_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("OPENAI_HIGH_MODELS", "claude-sonnet-4-6,gpt-5.5")

    selected = ProviderInventoryService._select_openai_probe_models(
        ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-flash", "qwen3.7-max"],
        default_model="gpt-5.4-mini",
        limit=3,
    )

    assert selected == ["gpt-5.4-mini", "claude-sonnet-4-6", "gpt-5.5"]


def test_provider_inventory_service_refreshes_openai_runtime_inventory(monkeypatch, tmp_path):
    calls = {}

    class _Registry:
        def get_models(self, force_refresh=False):
            calls["force_refresh"] = force_refresh
            return ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-flash"]

        def diagnostics(self):
            return {"ok": True, "source": "live", "endpoint": "https://codex.sale/v1/models"}

    async def _fake_probe(self, **kwargs):
        calls["probe_kwargs"] = kwargs
        return [
            {
                "model": "gpt-5.5",
                "chat_completions": {"ok": True, "status_code": 200, "error": None, "response_sample": "ok", "endpoint": "chat_completions"},
                "responses": {"ok": True, "status_code": 200, "error": None, "response_sample": "ok", "endpoint": "responses"},
                "fully_routable": True,
            },
            {
                "model": "claude-sonnet-4-6",
                "chat_completions": {"ok": True, "status_code": 200, "error": None, "response_sample": "ok", "endpoint": "chat_completions"},
                "responses": {"ok": False, "status_code": 400, "error": "unsupported", "response_sample": "", "endpoint": "responses"},
                "fully_routable": False,
            },
        ]

    monkeypatch.setattr("core.core.provider_inventory_service.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._probe_openai_runtime_matrix_async", _fake_probe)
    monkeypatch.setattr("core.core.provider_inventory_service.resolve_openai_provider_config", lambda: type("Cfg", (), {
        "api_key": "openai_usable_key_value_1234567890",
        "base_url": "https://codex.sale/v1",
        "models_endpoint": "https://codex.sale/v1/models",
        "chat_completions_endpoint": "https://codex.sale/v1/chat/completions",
        "responses_endpoint": "https://codex.sale/v1/responses",
        "default_model": "gpt-5.4-mini",
    })())

    def _fake_sync(models, *, base_url="", **kwargs):
        calls["models"] = list(models)
        calls["base_url"] = base_url
        calls["validated_rows"] = kwargs.get("validated_rows")
        return {"orchestrator_templates_path": "templates.json", "model_template_manifest": {"summary": {"routable_count": 1}, "models": [{"model_name": "gpt-5.5", "status": "routable"}]}}

    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", _fake_sync)
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(tmp_path / "openai_runtime_inventory.json"))

    service = ProviderInventoryService()
    payload = service.refresh_openai_runtime_inventory(force_refresh=True, probe_limit=2)

    assert calls["force_refresh"] is True
    assert calls["models"] == ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-flash"]
    assert payload["selected_model_count"] == 2
    assert payload["validated_model_count"] == 2
    assert payload["fully_routable_count"] == 1
    assert payload["fully_routable_models"] == ["gpt-5.5"]
    assert payload["artifact_sync"]["orchestrator_templates_path"] == "templates.json"
    assert calls["validated_rows"][0]["model"] == "gpt-5.5"
    assert payload["model_templates"]["summary"]["routable_count"] == 1


def test_provider_inventory_service_refreshes_openai_runtime_inventory_from_templates_when_registry_is_sparse(monkeypatch, tmp_path):
    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.5"]

        def diagnostics(self):
            return {"ok": True, "source": "cache_fallback", "error_type": "missing_api_key", "error_message": "missing key"}

    generated_root = tmp_path / "generated" / "openai_compatible"
    generated_root.mkdir(parents=True)
    (generated_root / "manifest.json").write_text(json.dumps({
        "models": ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-pro"],
    }), encoding="utf-8")
    (generated_root / "model_template_manifest.json").write_text(json.dumps({
        "models": [
            {"model_name": "gpt-5.5", "status": "discovered"},
            {"model_name": "claude-sonnet-4-6", "status": "discovered"},
            {"model_name": "deepseek-v4-pro", "status": "discovered"},
        ],
    }), encoding="utf-8")
    (generated_root / "orchestrator_templates.json").write_text(json.dumps({
        "roles": {
            "code_parallel": [
                {"model_name": "gpt-5.5"},
                {"model_name": "claude-sonnet-4-6"},
                {"model_name": "deepseek-v4-pro"},
            ]
        }
    }), encoding="utf-8")

    calls = {}

    def _fake_sync(models, *, base_url="", **kwargs):
        calls["models"] = list(models)
        return {
            "orchestrator_templates_path": str(generated_root / "orchestrator_templates.json"),
            "model_template_manifest": {
                "summary": {"total_models": len(models), "discovered_count": len(models)},
                "models": [{"model_name": model, "status": "discovered"} for model in models],
            },
        }

    monkeypatch.setattr("core.core.provider_inventory_service.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr("core.core.provider_inventory_service.resolve_openai_provider_config", lambda: type("Cfg", (), {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "models_endpoint": "https://api.openai.com/v1/models",
        "chat_completions_endpoint": "https://api.openai.com/v1/chat/completions",
        "responses_endpoint": "https://api.openai.com/v1/responses",
        "default_model": "gpt-5.5",
    })())
    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", _fake_sync)
    monkeypatch.setenv("OPENAI_GENERATED_PROFILE_DIR", str(generated_root))
    monkeypatch.setenv("OPENAI_MODEL_TEMPLATE_MANIFEST_PATH", str(generated_root / "model_template_manifest.json"))
    monkeypatch.setenv("OPENAI_ORCHESTRATOR_TEMPLATES_PATH", str(generated_root / "orchestrator_templates.json"))
    monkeypatch.setenv("OPENAI_MODELS_FULL_CACHE_PATH", str(tmp_path / "missing_openai_models_full.json"))
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(tmp_path / "openai_runtime_inventory.json"))

    service = ProviderInventoryService()
    payload = service.refresh_openai_runtime_inventory(force_refresh=True, probe_limit=3)

    assert payload["models"] == ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-pro"]
    assert payload["selected_models"] == ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-pro"]
    assert payload["validated_models"] == []
    assert payload["model_templates"]["summary"]["total_models"] == 3
    assert calls["models"] == ["gpt-5.5", "claude-sonnet-4-6", "deepseek-v4-pro"]


def test_provider_inventory_service_openai_entry_embeds_runtime_inventory(monkeypatch):
    monkeypatch.setattr(
        ProviderInventoryService,
        "refresh_openai_runtime_inventory",
        lambda self, **kwargs: {
            "registry_diagnostics": {"ok": True, "source": "live"},
            "artifact_sync": {"orchestrator_templates_path": "templates.json"},
            "models": ["gpt-5.5", "claude-sonnet-4-6"],
            "validated_models": [],
        },
    )

    service = ProviderInventoryService()
    entry = service._openai_entry(force_refresh=True)

    assert entry.models == ["gpt-5.5", "claude-sonnet-4-6"]
    assert entry.source == "live"
    assert entry.diagnostics["runtime_inventory"]["artifact_sync"]["orchestrator_templates_path"] == "templates.json"


def test_antigravity_profiles_are_direct_api_only():
    manifest = json.loads(Path("core/mimo/profiles/manifest.json").read_text(encoding="utf-8"))

    assert "providers/provider__antigravity.json" in manifest["provider_profiles"]
    assert "models/model__antigravity-pro.json" in manifest["model_profiles"]
    assert "models/model__antigravity-flash.json" in manifest["model_profiles"]
    assert "combinations/combo__antigravity__antigravity-pro.json" in manifest["combo_profiles"]

    for rel_path in [
        "providers/provider__antigravity.json",
        "models/model__antigravity-pro.json",
        "models/model__antigravity-flash.json",
        "combinations/combo__antigravity__antigravity-pro.json",
    ]:
        profile = json.loads((Path("core/mimo/profiles") / rel_path).read_text(encoding="utf-8"))
        assert profile["metadata"]["transport"] == "direct_api"
        assert profile["metadata"]["inventory_source"] == "direct_http_catalog"
        assert profile["metadata"]["auth_mode"] == "api_key"


def test_provider_inventory_service_refreshes_openai_runtime_inventory_from_discovery_key(monkeypatch, tmp_path):
    class _Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.content = b'{}'

        def json(self):
            return self._payload

    discovery = tmp_path / "openai_endpoint_discovery.json"
    discovery.write_text(
        '{"api_key":"openai_nonsecret_key_value_1234567890","base_url":"https://codex.sale/v1","default_model":"gpt-5.5","usable":true}',
        encoding="utf-8",
    )
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_SALE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_BRIDGE_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_SALE_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_ENDPOINT_DISCOVERY_PATH", str(discovery))
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))

    def _fake_get(url, headers=None, timeout=10.0, allow_redirects=False):
        assert url == "https://codex.sale/v1/models"
        assert headers == {"Authorization": "Bearer openai_nonsecret_key_value_1234567890"}
        return _Response(200, {"data": [{"id": "gpt-5.5"}, {"id": "text-embedding-3-large"}]})

    async def _fake_probe(self, **kwargs):
        assert kwargs["api_key"] == "openai_nonsecret_key_value_1234567890"
        assert kwargs["model_names"] == ["gpt-5.5"]
        return [{
            "model": "gpt-5.5",
            "chat_completions": {"ok": True, "status_code": 200},
            "responses": {"ok": True, "status_code": 200},
            "fully_routable": True,
        }]

    monkeypatch.setattr("core.core.openai_model_registry.requests.get", _fake_get)
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._probe_openai_runtime_matrix_async", _fake_probe)
    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", lambda *args, **kwargs: {"ok": True})

    service = ProviderInventoryService()
    payload = service.refresh_openai_runtime_inventory(force_refresh=True, probe_limit=1)

    assert payload["models"] == ["gpt-5.5"]
    assert payload["fully_routable_models"] == ["gpt-5.5"]
    assert payload["registry_diagnostics"]["discovery"]["usable"] is True
    assert payload["base_url"] == "https://codex.sale/v1"
    assert payload["provider_id"] == "codexsale"
    assert payload["provider_name"] == "Codex Sale"
    assert payload["messages_endpoint"] == "https://codex.sale/v1/messages"
    assert payload["codex_endpoint"] == "https://codex.sale/backend-api/codex"


def test_provider_inventory_service_builds_openai_endpoint_inventory_with_usage_and_suppression(monkeypatch):
    service = ProviderInventoryService()
    monkeypatch.setattr(service, "provider_snapshot", lambda provider: {
        "provider": "openai",
        "ok": True,
        "source": "live",
        "models": ["gpt-5.5", "claude-sonnet-4-6"],
        "diagnostics": {
            "runtime_inventory": {
                "model_templates": {
                    "models": [
                        {"model_name": "gpt-5.5", "status": "routable", "kernel_eligible": True, "fallback_candidate": True},
                        {"model_name": "claude-sonnet-4-6", "status": "blocked", "kernel_eligible": False, "fallback_candidate": False},
                    ]
                }
            }
        },
    })
    usage_snapshot = {
        "history": [
            {"provider": "openai", "model": "gpt-5.5", "tokens_used": 1200, "estimated_cost_usd": 0.42},
            {"provider": "openai", "model": "gpt-5.5", "tokens_used": 300, "estimated_cost_usd": 0.08},
        ],
        "stats": {
            "models": {
                "gpt-5.5": {
                    "limit_tokens": 10000,
                    "remaining_tokens": 8500,
                    "remaining_percentage": 85.0,
                    "used_percentage": 15.0,
                    "status": "ok",
                }
            }
        },
    }
    payload = service.build_provider_endpoint_inventory(
        "openai",
        usage_snapshot=usage_snapshot,
        suppression_snapshot={"openai": {"reason": "quota", "seconds_remaining": 120}},
    )

    assert payload["provider"] == "openai"
    assert payload["suppressed"] is True
    assert payload["usage"]["tokens_used"] == 1500
    assert payload["models"][0]["usage"]["tokens_used"] == 1500
    assert payload["summary"]["eligible_models"] == 1
    assert payload["summary"]["blocked_models"] == 1


def test_provider_inventory_service_runtime_inventory_embeds_model_template_manifest(monkeypatch, tmp_path):
    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.5", "claude-sonnet-4-6"]
        def diagnostics(self):
            return {"ok": True, "source": "live", "endpoint": "https://api.openai.com/v1/models"}

    async def _fake_probe(self, **kwargs):
        return [{
            "model": "gpt-5.5",
            "chat_completions": {"ok": True, "status_code": 200},
            "responses": {"ok": True, "status_code": 200},
            "fully_routable": True,
        }]

    def _fake_sync(models, *, base_url="", **kwargs):
        return {
            "orchestrator_templates_path": "templates.json",
            "model_template_manifest_path": "model_template_manifest.json",
            "model_template_manifest": {
                "summary": {"routable_count": 1, "discovered_count": 1},
                "models": [
                    {"model_name": "gpt-5.5", "status": "routable", "kernel_eligible": True},
                    {"model_name": "claude-sonnet-4-6", "status": "discovered", "kernel_eligible": False},
                ],
            },
        }

    monkeypatch.setattr("core.core.provider_inventory_service.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._probe_openai_runtime_matrix_async", _fake_probe)
    monkeypatch.setattr("core.core.provider_inventory_service.resolve_openai_provider_config", lambda: type("Cfg", (), {
        "api_key": "openai_nonsecret_key_value_1234567890",
        "base_url": "https://api.openai.com/v1",
        "models_endpoint": "https://api.openai.com/v1/models",
        "chat_completions_endpoint": "https://api.openai.com/v1/chat/completions",
        "responses_endpoint": "https://api.openai.com/v1/responses",
        "default_model": "gpt-5.5",
    })())
    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", _fake_sync)
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(tmp_path / "openai_runtime_inventory.json"))

    service = ProviderInventoryService()
    payload = service.refresh_openai_runtime_inventory(force_refresh=True, probe_limit=1)

    assert payload["model_templates"]["summary"]["routable_count"] == 1
    assert payload["model_templates"]["models"][0]["model_name"] == "gpt-5.5"
    assert payload["artifact_sync"]["model_template_manifest_path"] == "model_template_manifest.json"


def test_provider_inventory_service_openai_runtime_inventory_embeds_discovery(monkeypatch, tmp_path):
    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.5"]
        def diagnostics(self):
            return {"ok": True, "source": "live", "endpoint": "https://api.openai.com/v1/models"}

    async def _fake_probe(self, **kwargs):
        return [{
            "model": "gpt-5.5",
            "chat_completions": {"ok": True, "status_code": 200},
            "responses": {"ok": True, "status_code": 200},
            "fully_routable": True,
        }]

    monkeypatch.setattr("core.core.provider_inventory_service.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._probe_openai_runtime_matrix_async", _fake_probe)
    monkeypatch.setattr("core.core.provider_inventory_service.resolve_openai_provider_config", lambda: type("Cfg", (), {
        "api_key": "openai_nonsecret_key_value_1234567890",
        "base_url": "https://api.openai.com/v1",
        "models_endpoint": "https://api.openai.com/v1/models",
        "chat_completions_endpoint": "https://api.openai.com/v1/chat/completions",
        "responses_endpoint": "https://api.openai.com/v1/responses",
        "default_model": "gpt-5.5",
    })())
    monkeypatch.setattr("core.core.provider_inventory_service.load_openai_endpoint_discovery", lambda: {"source": "codex-sale.env", "base_url": "https://api.openai.com/v1", "usable": True})
    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(tmp_path / "openai_runtime_inventory.json"))

    service = ProviderInventoryService()
    payload = service.refresh_openai_runtime_inventory(force_refresh=True, probe_limit=1)

    assert payload["registry_diagnostics"]["discovery"]["source"] == "codex-sale.env"
    assert payload["fully_routable_models"] == ["gpt-5.5"]



def test_provider_inventory_service_collects_local_llm_runtime(monkeypatch):
    class _FakeRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        def check_health_sync(self, model_name=None):
            return type('Health', (), {
                'ok': True,
                'ready': True,
                'status': 'ready',
                'endpoint': 'http://127.0.0.1:11434',
                'model_name': model_name or 'qwen2.5:32b-instruct-q4_k_m',
                'available_models': ['qwen2.5:32b-instruct-q4_k_m', 'qwen2.5:0.5b'],
                'model_present': True,
                'latency_ms': 12.5,
                'attempts': 1,
                'status_code': 200,
                'error': None,
            })()

        def list_resident_models_sync(self):
            return [type('Resident', (), {'name': 'qwen2.5:32b-instruct-q4_k_m', 'size_vram': 3221225472})()]

    monkeypatch.setattr('core.core.provider_inventory_service.LocalModelRuntime', _FakeRuntime)

    service = ProviderInventoryService()
    payload = service.collect(force_refresh=True)['local_llm']

    assert payload['provider'] == 'local_llm'
    assert payload['ok'] is True
    assert payload['models'] == ['qwen2.5:32b-instruct-q4_k_m', 'qwen2.5:0.5b']
    assert payload['diagnostics']['resident_models'] == ['qwen2.5:32b-instruct-q4_k_m']
    assert payload['diagnostics']['gpu']['backend'] in {'auto', ''}


def test_provider_inventory_service_builds_local_llm_runtime_inventory(monkeypatch):
    class _FakeRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        def check_health_sync(self, model_name=None):
            return type('Health', (), {
                'ok': True,
                'ready': False,
                'status': 'degraded',
                'endpoint': 'http://127.0.0.1:11434',
                'model_name': model_name or 'qwen2.5:32b-instruct-q4_k_m',
                'available_models': ['qwen2.5:0.5b'],
                'model_present': False,
                'latency_ms': 17.0,
                'attempts': 1,
                'status_code': 200,
                'error': None,
            })()

        def list_resident_models_sync(self):
            return [type('Resident', (), {'name': 'qwen2.5:0.5b', 'size_vram': 1073741824})()]

    monkeypatch.setattr('core.core.provider_inventory_service.LocalModelRuntime', _FakeRuntime)

    service = ProviderInventoryService()
    payload = service.build_provider_runtime_inventory('local_llm', force_refresh=True)

    assert payload['provider'] == 'local_llm'
    assert payload['status'] == 'ready'
    assert payload['summary']['resident_models'] == 1
    assert payload['summary']['available_models'] == 1
    assert payload['models'][0]['model_name'] == 'qwen2.5:0.5b'
    assert payload['models'][0]['resident'] is True



def test_provider_inventory_service_refresh_provider_entry_updates_single_provider(monkeypatch):
    service = ProviderInventoryService()
    calls: list[str] = []

    monkeypatch.setattr(service, "_openai_entry", lambda **kwargs: calls.append("openai") or type("Entry", (), {"provider": "openai", "fetched_at": 1, "ok": True, "source": "live", "models": ["gpt-5.5"], "error": None, "status_code": None, "diagnostics": {}})())
    monkeypatch.setattr(service, "_mistral_entry", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected provider refresh")))
    monkeypatch.setattr(service, "_antigravity_entry", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected provider refresh")))
    monkeypatch.setattr(service, "_mimo_entry", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected provider refresh")))
    monkeypatch.setattr(service, "_local_llm_entry", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected provider refresh")))
    monkeypatch.setattr(service, "_ai_kernel_entry", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected provider refresh")))

    entry = service.refresh_provider_entry("openai", force_refresh=True)

    assert entry["provider"] == "openai"
    assert calls == ["openai"]



def test_provider_inventory_service_builds_search_index_for_fast_lookup(monkeypatch):
    service = ProviderInventoryService()
    monkeypatch.setattr(service, "collect", lambda force_refresh=False: {
        "openai": {"provider": "openai", "fetched_at": 1, "ok": True, "source": "live", "models": ["gpt-5.5"], "error": None, "status_code": None, "diagnostics": {}},
        "local_llm": {"provider": "local_llm", "fetched_at": 1, "ok": True, "source": "ollama_http", "models": ["qwen2.5:32b-instruct-q4_k_m"], "error": None, "status_code": 200, "diagnostics": {"resident_models": ["qwen2.5:32b-instruct-q4_k_m"]}},
        "antigravity": {"provider": "antigravity", "fetched_at": 1, "ok": False, "source": "registry", "models": [], "error": "inventory_unavailable", "status_code": None, "diagnostics": {}},
        "mistral": {"provider": "mistral", "fetched_at": 1, "ok": True, "source": "cache", "models": ["mistral-small"], "error": None, "status_code": 200, "diagnostics": {}},
        "mimo": {"provider": "mimo", "fetched_at": 1, "ok": True, "source": "live", "models": ["xiaomi/mimo-v2.5-pro"], "error": None, "status_code": 200, "diagnostics": {}},
        "ai_kernel": {"provider": "ai_kernel", "fetched_at": 1, "ok": False, "source": "disabled_by_env", "models": [], "error": "ai_kernel_disabled_by_env", "status_code": None, "diagnostics": {}},
    })

    service.refresh(force_refresh=True)
    summary = service.model_index_summary()
    row = service.find_model("qwen2.5:32b-instruct-q4_k_m")

    assert summary["total_models"] == 4
    assert summary["provider_counts"]["local_llm"] == 1
    assert row["provider"] == "local_llm"
    assert row["resident"] is True



def test_provider_inventory_service_antigravity_entry_uses_direct_catalog(monkeypatch):
    monkeypatch.setattr(
        "core.core.provider_inventory_service.fetch_antigravity_model_catalog",
        lambda force_refresh=False, timeout_sec=20.0: {
            "ok": True,
            "source": "live",
            "provider": "antigravity",
            "base_url": "https://antigravity.example/v1",
            "endpoint": "https://antigravity.example/v1/models",
            "status_code": 200,
            "models": ["antigravity-pro", "antigravity-flash"],
            "model_count": 2,
            "error": None,
            "generated_at": 123,
        },
    )

    service = ProviderInventoryService()
    entry = service._antigravity_entry(force_refresh=True)

    assert entry.ok is True
    assert entry.models == ["antigravity-pro", "antigravity-flash"]
    assert entry.diagnostics["default_model"] == "antigravity-pro"
    assert entry.diagnostics["model_alias_present"] is True



def test_provider_inventory_service_ai_kernel_entry_marks_alias_presence(monkeypatch):
    monkeypatch.setenv("AI_KERNEL_ENABLED", "true")
    monkeypatch.setenv("AI_KERNEL_MODEL_ALIAS", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m")
    monkeypatch.setattr(
        "core.core.ai_kernel_bridge.AIKernelBridge.gate",
        lambda self, model_name=None, ensure_ready=False: {
            "base_url": "http://127.0.0.1:8012/v1",
            "ready": False,
            "reachable": True,
            "model_alias_present": False,
            "models": ["other-model"],
            "probe": {"ok": True, "status_code": 200, "models": ["other-model"], "error": None},
            "attempted_autostart": ensure_ready,
            "service_process_active": True,
            "autostart_enabled": True,
            "manage_remote_enabled": False,
        },
    )

    service = ProviderInventoryService()
    entry = service._ai_kernel_entry(force_refresh=True)

    assert entry.ok is True
    assert entry.diagnostics["model_alias_present"] is False
    assert entry.diagnostics["inventory_status"] == "degraded"
    assert entry.diagnostics["attempted_autostart"] is True


def test_provider_inventory_service_openai_runtime_inventory_marks_partial_and_recommended_models(monkeypatch, tmp_path):
    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.4-mini", "gpt-5.5"]
        def diagnostics(self):
            return {"ok": True, "source": "live", "endpoint": "https://codex.sale/v1/models"}

    async def _fake_probe(self, **kwargs):
        return [
            {
                "model": "gpt-5.4-mini",
                "chat_completions": {"ok": True, "status_code": 200, "response_sample": "ok"},
                "responses": {"ok": False, "status_code": 429, "error": "rate limited"},
                "fully_routable": False,
            },
            {
                "model": "gpt-5.5",
                "chat_completions": {"ok": True, "status_code": 200, "response_sample": "ok"},
                "responses": {"ok": True, "status_code": 200, "response_sample": "ok"},
                "fully_routable": True,
            },
        ]

    def _fake_sync(models, *, base_url="", **kwargs):
        return {
            "model_template_manifest": {
                "models": [
                    {"model_name": "gpt-5.4-mini", "status": "chat_only", "role_scores": {"docs_primary": 0.9, "code_parallel": 0.8}, "preferred_task_types": ["docs"]},
                    {"model_name": "gpt-5.5", "status": "routable", "role_scores": {"docs_primary": 1.0, "code_parallel": 1.2}, "preferred_task_types": ["code", "review"]},
                ]
            }
        }

    monkeypatch.setattr("core.core.provider_inventory_service.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr("core.core.provider_inventory_service.ProviderInventoryService._probe_openai_runtime_matrix_async", _fake_probe)
    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", _fake_sync)
    monkeypatch.setattr("core.core.provider_inventory_service.resolve_openai_provider_config", lambda: type("Cfg", (), {
        "api_key": "openai_nonsecret_key_value_1234567890",
        "base_url": "https://codex.sale/v1",
        "models_endpoint": "https://codex.sale/v1/models",
        "chat_completions_endpoint": "https://codex.sale/v1/chat/completions",
        "responses_endpoint": "https://codex.sale/v1/responses",
        "messages_endpoint": "https://codex.sale/v1/messages",
        "messages_count_tokens_endpoint": "https://codex.sale/v1/messages/count_tokens",
        "codex_endpoint": "https://codex.sale/backend-api/codex",
        "default_model": "gpt-5.4",
    })())
    monkeypatch.setenv("AI_BRIDGE_OPENAI_PROVIDER_ID", "codex-sale")
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(tmp_path / "openai_runtime_inventory.json"))

    service = ProviderInventoryService()
    payload = service.refresh_openai_runtime_inventory(force_refresh=True, probe_limit=2)

    rows = {row["model"]: row for row in payload["validated_models"]}
    assert rows["gpt-5.4-mini"]["availability"] == "rate_limited"
    assert rows["gpt-5.4-mini"]["criteria"]["rate_limited"] is True
    assert rows["gpt-5.5"]["availability"] == "available"
    assert payload["recommended_models"]["roles"]["code_parallel"][0] == "gpt-5.5"
    assert payload["recommended_models"]["defaults"]["cheapest_routable"] == "gpt-5.5"
    assert payload["pricing"]["gpt-5.5"]["output_usd_per_1k"] > payload["pricing"]["gpt-5.4-mini"]["output_usd_per_1k"]


def test_provider_inventory_service_runtime_inventory_exposes_codexsale_identity(monkeypatch):
    service = ProviderInventoryService()
    monkeypatch.setattr(service, "refresh_openai_runtime_inventory", lambda **kwargs: {
        "provider_id": "codexsale",
        "provider_name": "Codex Sale",
        "fetched_at": 123,
        "base_url": "https://codex.sale/v1",
        "models_endpoint": "https://codex.sale/v1/models",
        "chat_completions_endpoint": "https://codex.sale/v1/chat/completions",
        "responses_endpoint": "https://codex.sale/v1/responses",
        "messages_endpoint": "https://codex.sale/v1/messages",
        "messages_count_tokens_endpoint": "https://codex.sale/v1/messages/count_tokens",
        "codex_endpoint": "https://codex.sale/backend-api/codex",
        "validated_model_count": 1,
        "fully_routable_count": 1,
        "fully_routable_models": ["gpt-5.4"],
        "validated_models": [{"model": "gpt-5.4", "available": True}],
        "recommended_models": {"defaults": {"best_overall": ["gpt-5.4"]}},
        "endpoint_manifest": {"provider_id": "codexsale"},
        "pricing": {"gpt-5.4": {"input_usd_per_1k": 0.0015}},
        "selected_models": ["gpt-5.4"],
        "model_templates": {},
    })
    monkeypatch.setattr(service, "build_provider_endpoint_inventory", lambda *args, **kwargs: {
        "provider": "openai",
        "status": "ready",
        "source": "live",
        "suppressed": False,
        "suppression": None,
        "usage": {"provider": "openai", "tokens_used": 0, "estimated_cost_usd": 0.0, "requests_count": 0, "models_tracked": 0},
        "summary": {"total_models": 1},
        "models": [{"model_name": "gpt-5.4", "status": "routable"}],
        "diagnostics": {},
    })

    payload = service.build_provider_runtime_inventory("openai")

    assert payload["provider_id"] == "codexsale"
    assert payload["provider_name"] == "Codex Sale"
    assert payload["endpoints"]["codex_endpoint"] == "https://codex.sale/backend-api/codex"
    assert payload["recommended_models"]["defaults"]["best_overall"] == ["gpt-5.4"]
    assert payload["pricing"]["gpt-5.4"]["input_usd_per_1k"] == 0.0015
