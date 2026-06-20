from __future__ import annotations

from core.core.provider_inventory_service import ProviderInventoryService


def test_provider_inventory_service_reads_provider_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "provider_inventory_snapshot.json"
    snapshot.write_text('{"updated_at": 123, "providers": {"mistral": {"provider": "mistral", "models": ["mistral-large-latest"], "source": "cache", "ok": true}}}', encoding="utf-8")
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
