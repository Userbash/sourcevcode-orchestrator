from __future__ import annotations

import json
import time

from core.core.provider_inventory_service import ProviderInventoryService


def test_provider_inventory_service_reads_provider_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "provider_inventory_snapshot.json"
    snapshot.write_text(json.dumps({"updated_at": int(time.time()), "providers": {"mistral": {"provider": "mistral", "models": ["mistral-large-latest"], "source": "cache", "ok": True}}}), encoding="utf-8")
    monkeypatch.setenv("PROVIDER_INVENTORY_SNAPSHOT_PATH", str(snapshot))

    service = ProviderInventoryService()
    provider = service.provider_snapshot("mistral")

    assert provider["provider"] == "mistral"
    assert provider["models"] == ["mistral-large-latest"]
    assert provider["source"] == "cache"


def test_provider_inventory_service_builds_participation_snapshot(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "model_ping_report.json").write_text(
        '{"openai": {"models": [{"model": "gpt-5.4", "ok": true}, {"model": "gpt-5.5", "ok": true}]}, "mistral": {"models": [{"model": "mistral-large-latest", "ok": true}, {"model": "codestral-embed", "ok": false, "skipped": true, "skip_reason": "embedding_model"}, {"model": "labs-leanstral-2603", "ok": false, "status_code": 403, "error": "Labs model"}]}, "local_llm": {"models": [{"model": "qwen2.5:32b-instruct-q4_k_m", "ok": true}, {"model": "qwen-2.5-7b-instruct", "ok": false, "error": ""}]}, "antigravity": {"models": [{"model": "antigravity-cli", "ok": false, "error": "not found"}]}}',
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
    snap = service.build_participation_snapshot([_Record("mistral", "mistral-large-latest"), _Record("local", "qwen2.5:32b-instruct-q4_k_m"), _Record("google", "antigravity-cli")])

    assert any(item["model_name"] == "mistral-large-latest" and item["source"] == "registered_agent" for item in snap["active_now"])
    assert any(item["model_name"] == "mistral/mistral-large-latest" and item["source"] == "mimo_usable" for item in snap["active_now"])
    assert any(item["model_name"] == "gpt-5.5" for item in snap["available_but_not_wired_directly"])
    assert any(item["model_name"] == "codestral-embed" and item["reason"] == "embedding_model" for item in snap["present_but_unusable"])
    assert any(item["model_name"] == "github-copilot/gpt-5.2" and item["reason"] == "github_pat_not_supported" for item in snap["present_but_unusable"])
    assert any(item["model_name"] == "qwen-2.5-7b-instruct" and item["reason"] == "probe_failed" for item in snap["present_but_unusable"])


def test_provider_inventory_service_syncs_openai_artifacts(monkeypatch):
    calls = {}

    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.5", "claude-sonnet-4-6"]
        def diagnostics(self):
            return {"ok": True, "source": "live"}

    monkeypatch.setattr("core.core.provider_inventory_service.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr("core.core.provider_inventory_service.resolve_openai_provider_config", lambda: type("Cfg", (), {"base_url": "https://codex.sale/v1"})())

    def _fake_sync(models, *, base_url=''):
        calls["models"] = list(models)
        calls["base_url"] = base_url
        return {"generated_profile_root": "generated", "orchestrator_templates_path": "templates.json"}

    monkeypatch.setattr("core.core.provider_inventory_service.sync_openai_compatible_artifacts", _fake_sync)

    service = ProviderInventoryService()
    entry = service.collect(force_refresh=True)["openai"]

    assert calls["models"] == ["gpt-5.5", "claude-sonnet-4-6"]
    assert calls["base_url"] == "https://codex.sale/v1"
    assert entry["diagnostics"]["artifact_sync"]["orchestrator_templates_path"] == "templates.json"


def test_provider_inventory_service_mimo_uses_sync_refresh_in_running_loop(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    monkeypatch.setenv("PROVIDER_INVENTORY_REPORT_DIR", str(report_dir))
    class _Bridge:
        def __init__(self):
            self.is_cli_alive = True
        def get_cached_models(self):
            return []
        def refresh_cache_sync(self):
            return [type("Snap", (), {"full_id": "mimo/mimo-auto", "id": "mimo-auto"})()]

    monkeypatch.setattr("core.core.provider_inventory_service.MimoAsyncBridge", _Bridge)
    monkeypatch.setattr("core.core.provider_inventory_service.asyncio.get_running_loop", lambda: object())

    service = ProviderInventoryService()
    entry = service._mimo_entry(force_refresh=True)

    assert entry.ok is True
    assert entry.source == "bridge_live_sync"
    assert entry.models == ["mimo/mimo-auto"]


def test_provider_inventory_service_mimo_falls_back_to_generated_catalog(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    monkeypatch.setenv("PROVIDER_INVENTORY_REPORT_DIR", str(report_dir))
    cache = tmp_path / "openai_models_full.json"
    cache.write_text(json.dumps({"models": ["mimo-v2-pro", "mimo-v2.5", "gpt-5.5"]}), encoding="utf-8")
    monkeypatch.setenv("OPENAI_MODELS_FULL_CACHE_PATH", str(cache))

    class _Bridge:
        def __init__(self):
            self.is_cli_alive = False
        def get_cached_models(self):
            return []
        def refresh_cache_sync(self):
            return []

    monkeypatch.setattr("core.core.provider_inventory_service.MimoAsyncBridge", _Bridge)

    service = ProviderInventoryService()
    entry = service._mimo_entry(force_refresh=True)

    assert entry.ok is False
    assert entry.source == "generated_manifest_fallback"
    assert entry.models == ["mimo-v2-pro", "mimo-v2.5"]
    assert entry.diagnostics["generated_fallback_used"] is True



def test_provider_inventory_service_refreshes_mimo_usable_snapshot(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    monkeypatch.setenv("PROVIDER_INVENTORY_REPORT_DIR", str(report_dir))
    monkeypatch.setenv("AI_BRIDGE_MIMO_AUTO_PING_LIMIT", "3")

    class _Bridge:
        def __init__(self):
            self.is_cli_alive = True
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

    monkeypatch.setattr("core.core.provider_inventory_service.MimoAsyncBridge", _Bridge)
    monkeypatch.setattr("core.core.provider_inventory_service.subprocess.run", _fake_run)

    service = ProviderInventoryService()
    result = service.refresh_mimo_usable_snapshot(force_refresh=True)

    assert result["status"] == "ok"
    assert result["probed_count"] == 3
    payload = json.loads((report_dir / "mimo_usable_models.json").read_text(encoding="utf-8"))
    assert payload["usable_count"] == 3
    assert payload["models"][0]["model"] == "xiaomi/mimo-v2.5-pro"


def test_provider_inventory_service_normalizes_mimo_aliases():
    assert ProviderInventoryService._normalize_provider("xiaomi") == "mimo"
    assert ProviderInventoryService._normalize_provider("github-models") == "mimo"


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

    service = ProviderInventoryService()

    def _fake_refresh(*, force_refresh=False):
        assert force_refresh is True
        return {"mimo": {"provider": "mimo", "models": ["fresh-model"], "source": "bridge_live_sync", "ok": True}}

    service.refresh = _fake_refresh
    provider = service.provider_snapshot("mimo")

    assert provider["models"] == ["fresh-model"]
    assert provider["source"] == "bridge_live_sync"


def test_provider_inventory_service_mimo_merges_artifact_models_into_inventory(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "mimo_model_ping_report.json").write_text(json.dumps({
        "models": [{"model": "github-models/gpt-5.4", "ok": False, "error": "auth"}]
    }), encoding="utf-8")
    (report_dir / "mimo_usable_models.json").write_text(json.dumps({
        "models": [{"model": "xiaomi/mimo-v2.5-pro", "ok": True}]
    }), encoding="utf-8")
    monkeypatch.setenv("PROVIDER_INVENTORY_REPORT_DIR", str(report_dir))

    class _Bridge:
        def __init__(self):
            self.is_cli_alive = True
        def get_cached_models(self):
            return [type("Snap", (), {"full_id": "mimo/mimo-auto", "id": "mimo-auto"})()]
        def refresh_cache_sync(self):
            return self.get_cached_models()

    monkeypatch.setattr("core.core.provider_inventory_service.MimoAsyncBridge", _Bridge)

    service = ProviderInventoryService()
    entry = service._mimo_entry(force_refresh=False)

    assert entry.models == ["mimo/mimo-auto", "xiaomi/mimo-v2.5-pro", "github-models/gpt-5.4"]
    assert entry.diagnostics["auto_added_models_count"] == 2
    assert entry.diagnostics["usable_artifact_models_count"] == 1
    assert entry.diagnostics["ping_artifact_models_count"] == 1
    assert entry.source == "bridge_cache+artifact_merge"


def test_provider_inventory_service_reads_mimo_models_from_generated_profiles(tmp_path, monkeypatch):
    generated_root = tmp_path / "generated" / "openai_compatible"
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
    monkeypatch.setenv("OPENAI_GENERATED_PROFILE_DIR", str(generated_root))
    monkeypatch.setenv("OPENAI_MODELS_FULL_CACHE_PATH", str(tmp_path / "missing_cache.json"))

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

    def _fake_sync(models, *, base_url=""):
        calls["models"] = list(models)
        calls["base_url"] = base_url
        return {"orchestrator_templates_path": "templates.json"}

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
