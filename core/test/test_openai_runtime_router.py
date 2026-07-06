from __future__ import annotations

import json

import pytest

from core.core.model_selector import ModelSelector
from core.core.models import Complexity, Priority, Task, TaskContext, TaskInput, TaskType
from core.core.openai_model_registry import OpenAIModelRegistry
from core.core.openai_runtime_router import OpenAIRuntimeRouter


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
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
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
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_MODELS_CACHE_PATH", str(tmp_path / "openai_models.json"))

    class _Response:
        status_code = 502
        content = b''

    monkeypatch.setattr("core.core.openai_model_registry.requests.get", lambda *args, **kwargs: _Response())
    registry = OpenAIModelRegistry()

    assert registry.get_models(force_refresh=True) == []
    assert registry.diagnostics()["error_type"] == "endpoint_unavailable"
    assert registry.diagnostics()["status_code"] == 502


def test_openai_runtime_router_filters_to_fully_routable_models(tmp_path, monkeypatch):
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        json.dumps({
            "fully_routable_models": ["gpt-5.5", "gpt-5.4"],
            "validated_models": [
                {"model": "claude-opus-4-6", "chat_completions": {"ok": True}, "responses": {"ok": False}},
                {"model": "gpt-5.5", "chat_completions": {"ok": True}, "responses": {"ok": True}},
                {"model": "gpt-5.4", "chat_completions": {"ok": True}, "responses": {"ok": True}},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("OPENAI_HIGH_MODELS", "claude-opus-4-6,gpt-5.5,gpt-5.4")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()

    plan = router.build_plan(_task(complexity=Complexity.HIGH), "route only verified models")

    assert "claude-opus-4-6" not in plan.models
    assert plan.models[:2] == ["gpt-5.5", "gpt-5.4"]


def test_openai_runtime_router_filters_non_chat_models_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_HIGH_MODELS", "gpt-4o-transcribe,gpt-5.5,gpt-5.4")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()

    plan = router.build_plan(_task(complexity=Complexity.HIGH), "route only chat models")

    assert "gpt-4o-transcribe" not in plan.models
    assert plan.models[0] in {"gpt-5.5", "gpt-5.4"}


def test_openai_runtime_router_filters_runtime_incompatible_models(tmp_path, monkeypatch):
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        json.dumps({
            "validated_models": [
                {
                    "model": "claude-sonnet-4-6",
                    "chat_completions": {"ok": False, "error": "Claude pool has no eligible resources"},
                    "responses": {"ok": False, "error": "Claude pool has no eligible resources"},
                },
                {
                    "model": "gpt-5.5",
                    "chat_completions": {"ok": True},
                    "responses": {"ok": True},
                },
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("OPENAI_HIGH_MODELS", "claude-sonnet-4-6,gpt-5.5")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "false")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()

    plan = router.build_plan(_task(complexity=Complexity.HIGH), "avoid runtime-incompatible models")

    assert "claude-sonnet-4-6" not in plan.models
    assert plan.models[0] == "gpt-5.5"


def test_openai_runtime_router_sanitize_model_rejects_runtime_ineligible_model(tmp_path, monkeypatch):
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        json.dumps({
            "fully_routable_models": ["gpt-5.5"],
            "validated_models": [
                {"model": "claude-haiku-4-5", "chat_completions": {"ok": False, "error": "Claude pool has no eligible resources"}, "responses": {"ok": False, "error": "Claude pool has no eligible resources"}},
                {"model": "gpt-5.5", "chat_completions": {"ok": True}, "responses": {"ok": True}},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true")

    assert OpenAIRuntimeRouter.sanitize_model("claude-haiku-4-5") is None
    assert OpenAIRuntimeRouter.sanitize_model("gpt-5.5") == "gpt-5.5"


def test_openai_runtime_router_prefers_runtime_recommended_models(tmp_path, monkeypatch):
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        json.dumps({
            "fully_routable_models": ["gpt-5.5", "gpt-5.4-mini"],
            "recommended_models": {
                "roles": {
                    "code_parallel": ["gpt-5.5", "gpt-5.4-mini"],
                    "docs_primary": ["gpt-5.4-mini", "gpt-5.5"]
                },
                "defaults": {
                    "best_overall": ["gpt-5.5"],
                    "economy": ["gpt-5.4-mini"],
                    "premium": ["gpt-5.5"]
                }
            },
            "validated_models": [
                {"model": "gpt-5.5", "chat_completions": {"ok": True}, "responses": {"ok": True}},
                {"model": "gpt-5.4-mini", "chat_completions": {"ok": True}, "responses": {"ok": True}}
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("OPENAI_HIGH_MODELS", "gpt-5.4-mini,gpt-5.5")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()

    plan = router.build_plan(_task(complexity=Complexity.HIGH), "prefer recommended code model")

    assert plan.models[0] == "gpt-5.5"


def test_openai_runtime_router_prefers_runtime_economy_recommendations(tmp_path, monkeypatch):
    runtime_inventory = tmp_path / "openai_runtime_inventory.json"
    runtime_inventory.write_text(
        json.dumps({
            "fully_routable_models": ["gpt-5.5", "gpt-5.4-mini"],
            "recommended_models": {
                "roles": {
                    "docs_primary": ["gpt-5.5", "gpt-5.4-mini"]
                },
                "defaults": {
                    "best_overall": ["gpt-5.5"],
                    "economy": ["gpt-5.4-mini"],
                    "premium": ["gpt-5.5"]
                }
            },
            "validated_models": [
                {"model": "gpt-5.5", "chat_completions": {"ok": True}, "responses": {"ok": True}},
                {"model": "gpt-5.4-mini", "chat_completions": {"ok": True}, "responses": {"ok": True}}
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_RUNTIME_INVENTORY_PATH", str(runtime_inventory))
    monkeypatch.setenv("OPENAI_LOW_MODELS", "gpt-5.5,gpt-5.4-mini")
    monkeypatch.setenv("AI_BRIDGE_OPENAI_REQUIRE_ROUTABLE_MODELS", "true")
    OpenAIRuntimeRouter._session_token_usage.clear()
    router = OpenAIRuntimeRouter()
    task = _task(TaskType.DOCS, Complexity.LOW)
    task.routing_hints = {"source": "websocket", "cost_tier": "economy"}

    plan = router.build_plan(task, "prefer economy docs model")

    assert plan.models[0] == "gpt-5.4-mini"
