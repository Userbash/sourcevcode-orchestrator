from __future__ import annotations

from core.scripts import verify_provider_stack


def test_local_llm_summary_uses_configured_local_llm_module(monkeypatch):
    class _Module:
        def check_health(self):
            return {"ok": True, "status_code": 200, "available_models": ["qwen2.5:0.5b", "qwen2.5:32b-instruct-q4_k_m"], "model_present": True, "error": None}
    monkeypatch.setattr(verify_provider_stack, "LocalLLMModule", lambda: _Module())
    summary = verify_provider_stack._local_llm_summary()
    assert summary["ready"] is True
    assert summary["model_count"] == 2


def test_mimo_run_summary_prefers_working_native_model(monkeypatch):
    monkeypatch.setattr(verify_provider_stack, "configured_native_mimo_models", lambda: ["xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5"])
    def fake_invoke(model, prompt, timeout_sec=30.0, max_completion_tokens=128, temperature=0.0):
        if model == "xiaomi/mimo-v2.5":
            return ({"choices": [{"message": {"content": "ok"}}]}, None, 200)
        return (None, "Invalid API Key", 401)
    monkeypatch.setattr(verify_provider_stack, "invoke_mimo_native", fake_invoke)
    summary = verify_provider_stack._mimo_run_summary()
    assert summary["run_ready"] is True
    assert summary["run_model"] == "xiaomi/mimo-v2.5"
    assert summary["attempted_models"] == ["xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5"]


def test_verify_provider_stack_reports_inventory_snapshot(monkeypatch, capsys):
    class _Inventory:
        def read_snapshot(self):
            return {"updated_at": 123, "providers": {"mistral": {"models": ["mistral-large-latest"], "source": "cache"}, "antigravity": {"models": ["antigravity-flash"], "source": "registry"}}}
    class _Mistral:
        def probe_models(self):
            return {"ok": True, "status_code": 200, "models": ["mistral-large-latest"], "error": None, "inventory_source": "live"}
    class _Antigravity:
        def status(self):
            return {"ready": True, "auth_mode": "api_key", "inventory_ok": True, "inventory_source": "registry", "inventory_probe_kind": "inventory", "failure_kind": "", "models": ["antigravity-flash"], "api_probe": {"status_code": 200}, "generation_probe": {}, "models_probe": {}, "auth_probe": {}}
    monkeypatch.setattr(verify_provider_stack, "ProviderInventoryService", _Inventory)
    monkeypatch.setattr(verify_provider_stack, "MistralManager", _Mistral)
    monkeypatch.setattr(verify_provider_stack, "AntigravityManager", _Antigravity)
    monkeypatch.setattr(verify_provider_stack, "build_openai_summary", lambda: {"ready": True, "usable_by_policy": True})
    monkeypatch.setattr(verify_provider_stack, "_mimo_summary", lambda: {"ready": False})
    monkeypatch.setattr(verify_provider_stack, "_mimo_run_summary", lambda: {"run_ready": False, "attempted_models": []})
    monkeypatch.setattr(verify_provider_stack, "_mimo_native_run_summary", lambda: {"run_ready": False, "attempted_models": []})
    monkeypatch.setattr(verify_provider_stack, "_local_llm_summary", lambda: {"ready": True, "model_count": 1})
    monkeypatch.setattr(verify_provider_stack, "credential_snapshot", lambda envs: {"configured": True, "usable": True, "placeholder": False, "env_var": envs[0]})
    monkeypatch.setattr("sys.argv", ["verify_provider_stack.py"])
    verify_provider_stack.main()
    output = capsys.readouterr().out
    assert '"inventory_snapshot"' in output
    assert '"snapshot_model_count": 1' in output


def test_verify_provider_stack_uses_mimo_credential_snapshot(monkeypatch, capsys):
    class _Inventory:
        def read_snapshot(self):
            return {"updated_at": 123, "providers": {}}
    class _Mistral:
        def probe_models(self):
            return {"ok": True, "status_code": 200, "models": [], "error": None, "inventory_source": "live"}
    class _Antigravity:
        def status(self):
            return {"ready": True, "auth_mode": "api_key", "inventory_ok": True, "inventory_source": "registry", "inventory_probe_kind": "inventory", "failure_kind": "", "models": [], "api_probe": {"status_code": 200}, "generation_probe": {}, "models_probe": {}, "auth_probe": {}}
    def fake_credential_snapshot(envs):
        env_names = tuple(envs)
        if env_names == ("MIMO_API_KEY", "AI_BRIDGE_MIMO_API_KEY"):
            return {"configured": True, "usable": True, "placeholder": False, "env_var": "MIMO_API_KEY"}
        if env_names == ("GITHUB_API", "GITHUB_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "HOST_BRIDGE_GH_TOKEN"):
            return {"configured": False, "usable": False, "placeholder": False, "env_var": None}
        return {"configured": True, "usable": True, "placeholder": False, "env_var": env_names[0]}
    monkeypatch.setattr(verify_provider_stack, "ProviderInventoryService", _Inventory)
    monkeypatch.setattr(verify_provider_stack, "MistralManager", _Mistral)
    monkeypatch.setattr(verify_provider_stack, "AntigravityManager", _Antigravity)
    monkeypatch.setattr(verify_provider_stack, "build_openai_summary", lambda: {"ready": True, "usable_by_policy": True})
    monkeypatch.setattr(verify_provider_stack, "_mimo_summary", lambda: {"ready": True})
    monkeypatch.setattr(verify_provider_stack, "_mimo_run_summary", lambda: {"run_ready": True, "attempted_models": [], "run_model": "xiaomi/mimo-v2.5-pro", "run_response_sample": "ok"})
    monkeypatch.setattr(verify_provider_stack, "_mimo_native_run_summary", lambda: {"run_ready": True, "attempted_models": [], "run_model": "xiaomi/mimo-v2.5-pro", "run_response_sample": "ok"})
    monkeypatch.setattr(verify_provider_stack, "_local_llm_summary", lambda: {"ready": True, "model_count": 1})
    monkeypatch.setattr(verify_provider_stack, "credential_snapshot", fake_credential_snapshot)
    monkeypatch.setattr("sys.argv", ["verify_provider_stack.py"])
    verify_provider_stack.main()
    output = capsys.readouterr().out
    assert '"credential_env": "MIMO_API_KEY"' in output
    assert '"github_token_configured": false' in output


def test_ai_kernel_summary_respects_disabled_env(monkeypatch):
    monkeypatch.setenv("AI_KERNEL_ENABLED", "false")

    summary = verify_provider_stack._ai_kernel_summary()

    assert summary["configured"] is False
    assert summary["error"] == "ai_kernel_disabled_by_env"
