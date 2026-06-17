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
