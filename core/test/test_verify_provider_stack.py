from __future__ import annotations

from core.scripts import verify_provider_stack


def test_local_llm_summary_uses_configured_local_llm_module(monkeypatch):
    class _Module:
        def check_health(self):
            return {
                "ok": True,
                "status_code": 200,
                "available_models": ["qwen2.5:0.5b", "qwen2.5:32b-instruct-q4_k_m"],
                "model_present": True,
                "error": None,
            }

    monkeypatch.setattr(verify_provider_stack, "LocalLLMModule", lambda: _Module())

    summary = verify_provider_stack._local_llm_summary()

    assert summary == {
        "configured": True,
        "ready": True,
        "error": None,
        "model_count": 2,
        "sample_models": ["qwen2.5:0.5b", "qwen2.5:32b-instruct-q4_k_m"],
        "model_present": True,
        "status_code": 200,
    }


def test_mimo_run_summary_prefers_working_inventory_model(monkeypatch):
    inventory = """mimo/mimo-auto
{}
openai/gpt-5.4
{}
"""

    calls = []

    class _Proc:
        def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(args, capture_output=True, text=True, timeout=0, check=False):
        calls.append(args)
        if args == ["/usr/bin/mimo", "models", "--verbose"]:
            return _Proc(stdout=inventory)
        if args == ["/usr/bin/mimo", "run", "-m", "openai/gpt-5.4", "--format", "json", "reply with ok"]:
            return _Proc(stdout='''{"type":"text","part":{"text":"ok"}}\n''')
        raise AssertionError(args)

    monkeypatch.setattr(verify_provider_stack, "resolve_mimo_cli", lambda: "/usr/bin/mimo")
    monkeypatch.setattr(verify_provider_stack.subprocess, "run", fake_run)

    summary = verify_provider_stack._mimo_run_summary()

    assert summary["run_ready"] is True
    assert summary["run_model"] == "openai/gpt-5.4"
    assert summary["run_response_sample"] == "ok"
    assert summary["attempted_models"] == ["openai/gpt-5.4-mini", "openai/gpt-5.4"]
    assert calls[0] == ["/usr/bin/mimo", "models", "--verbose"]


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
    monkeypatch.setattr(verify_provider_stack, "_local_llm_summary", lambda: {"ready": True, "model_count": 1})
    monkeypatch.setattr(verify_provider_stack, "credential_snapshot", lambda envs: {"configured": True, "usable": True, "placeholder": False, "env_var": envs[0]})

    monkeypatch.setattr("sys.argv", ["verify_provider_stack.py"])
    verify_provider_stack.main()
    output = capsys.readouterr().out

    assert '"inventory_snapshot"' in output
    assert '"snapshot_model_count": 1' in output
