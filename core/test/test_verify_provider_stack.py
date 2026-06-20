from __future__ import annotations

from core.scripts import verify_provider_stack


def test_local_llm_summary_uses_configured_local_llm_module(monkeypatch):
    class _Module:
        def check_health(self):
            return {
                "ok": True,
                "status_code": 200,
                "available_models": ["qwen2.5:0.5b", "deepseek-r1:14b"],
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
        "sample_models": ["qwen2.5:0.5b", "deepseek-r1:14b"],
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
