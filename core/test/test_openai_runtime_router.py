from __future__ import annotations

import json

from core.core.model_selector import ModelSelector
from core.core.models import Complexity, Priority, Task, TaskContext, TaskInput, TaskType
from core.core.openai_model_registry import OpenAIModelRegistry
from core.core.openai_runtime_router import OpenAIRuntimeRouter


def _task(task_type: TaskType = TaskType.CODE, complexity: Complexity = Complexity.MEDIUM) -> Task:
    task = Task(
        task_type,
        TaskInput("implement a focused routing change", files=[]),
        TaskContext("hebrew-web", ".", "main"),
    )
    task.complexity = complexity
    return task


def test_openai_registry_uses_cached_text_models(tmp_path, monkeypatch):
    cache = tmp_path / "openai_models.json"
    cache.write_text(
        json.dumps(
            {
                "ts": 4_102_444_800,
                "models": ["gpt-5.4-mini", "gpt-5.4-nano", "text-embedding-3-small", "gpt-5.5"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_MODELS_CACHE_PATH", str(cache))
    monkeypatch.setenv("OPENAI_MODELS_CACHE_TTL_SEC", "999999999")

    catalog = OpenAIModelRegistry().get_catalog()

    assert "gpt-5.4-mini" in catalog.mini
    assert "gpt-5.4-nano" in catalog.nano
    assert "gpt-5.5" in catalog.standard




def test_openai_registry_exposes_fetch_error_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setenv("OPENAI_MODELS_CACHE_PATH", str(tmp_path / "openai_models.json"))

    def fake_fetch(self):
        self._last_diagnostics = type(self._last_diagnostics)(
            ok=False,
            error_type="AuthenticationError",
            error_message="invalid_api_key",
        )
        return []

    monkeypatch.setattr(OpenAIModelRegistry, "_fetch_live", fake_fetch)
    registry = OpenAIModelRegistry()

    assert registry.get_models(force_refresh=True) == []
    diagnostics = registry.diagnostics()
    assert diagnostics["ok"] is False
    assert diagnostics["error_type"] == "AuthenticationError"
    assert diagnostics["error_message"] == "invalid_api_key"

def test_openai_runtime_router_prefers_light_model_for_low_budget(monkeypatch):
    monkeypatch.setenv("OPENAI_SESSION_TOKEN_BUDGET", "64")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()

    plan = router.build_plan(_task(complexity=Complexity.CRITICAL), "very long prompt" * 100)

    assert plan.reason == "budget_guard_lightweight"
    assert plan.models[0] in {"gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-5.5"}


def test_model_selector_openai_auto_is_opt_in(monkeypatch):
    task = _task(TaskType.REVIEW, Complexity.HIGH)
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_SALE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_SALE_BASE_URL", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    legacy = ModelSelector().select(task)
    assert legacy.provider == "local"
    assert legacy.model_name == "qwen-2.5-7b-instruct"

    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setenv("OPENAI_HIGH_MODELS", "gpt-5.4-mini,gpt-5.4")
    auto = ModelSelector().select(task)

    assert auto.provider == "openai"
    assert auto.model_name == "gpt-5.4-mini"
    assert auto.reason.startswith("openai_auto_")


def test_model_selector_auto_falls_back_when_openai_key_is_placeholder(monkeypatch):
    task = _task(TaskType.REVIEW, Complexity.HIGH)
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "example_openai_key")
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_usable_key_value_1234567890")

    choice = ModelSelector().select(task)

    assert choice.provider == "mistral"
    assert choice.model_name == "mistral-large-latest"
    assert choice.reason.startswith("openai_auto_no_key_mistral_fallback")


def test_model_selector_auto_falls_back_when_openai_key_missing(monkeypatch):
    task = _task(TaskType.REVIEW, Complexity.HIGH)
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_SALE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_SALE_BASE_URL", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")

    choice = ModelSelector().select(task)

    assert choice.provider == "mistral"
    assert choice.model_name == "mistral-large-latest"
    assert choice.reason.startswith("openai_auto_no_key_mistral_fallback")


def test_provider_budget_router_honors_critical_mistral_fallback(monkeypatch):
    from core.core.provider_budget_router import ProviderBudgetRouter

    task = _task(TaskType.CODE, Complexity.CRITICAL)
    task.priority = Priority.CRITICAL
    monkeypatch.setenv("AI_BRIDGE_OPENAI_AUTO_MODEL", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_SALE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_SALE_BASE_URL", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")
    choice = ModelSelector().select(task)

    providers = ProviderBudgetRouter().preferred_providers(task, choice)

    assert choice.provider == "mistral"
    assert providers[:3] == ["mistral", "antigravity", "local"]


def test_openai_runtime_router_uses_websocket_budget_and_interactive_reason(monkeypatch):
    monkeypatch.setenv("OPENAI_SESSION_TOKEN_BUDGET", "120000")
    monkeypatch.setenv("OPENAI_SESSION_TOKEN_BUDGET_WS", "2000")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()
    task = _task(complexity=Complexity.MEDIUM)
    task.session_id = "ws-runtime-1"
    task.routing_hints = {"source": "websocket", "cost_tier": "interactive"}

    plan = router.build_plan(task, "short prompt")

    assert plan.remaining_tokens == 2000
    assert plan.reason == "ws_interactive"
    assert plan.models[0] in {"gpt-5.4-mini", "gpt-5.4", "gpt-5.5", "gpt-5.4-nano"}


def test_openai_runtime_router_uses_websocket_economy_models(monkeypatch):
    monkeypatch.setenv("OPENAI_SESSION_TOKEN_BUDGET_WS_ECONOMY", "5000")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()
    task = _task(complexity=Complexity.LOW)
    task.session_id = "ws-runtime-2"
    task.routing_hints = {"source": "websocket", "cost_tier": "economy"}

    plan = router.build_plan(task, "small")

    assert plan.remaining_tokens == 5000
    assert plan.reason == "ws_economy"
    assert plan.models[0] in {"gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-5.5"}



def test_openai_registry_reports_http_endpoint_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setenv("OPENAI_MODELS_CACHE_PATH", str(tmp_path / "openai_models.json"))

    class _Response:
        status_code = 502
        content = b''

    monkeypatch.setattr("core.core.openai_model_registry.requests.get", lambda *args, **kwargs: _Response())
    registry = OpenAIModelRegistry()

    assert registry.get_models(force_refresh=True) == []
    assert registry.diagnostics()["error_type"] == "endpoint_unavailable"
    assert registry.diagnostics()["status_code"] == 502
