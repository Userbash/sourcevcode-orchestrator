from __future__ import annotations

from core.core.orchestration_config import OrchestrationConfig
from core.core.control_profiles import ControlProfileRegistry


def test_control_profiles_load_from_json_manifest():
    registry = ControlProfileRegistry()

    manual = registry.get("manual")
    full_auto = registry.get("full_auto")

    assert manual.name == "MANUAL"
    assert manual.slug == "manual"
    assert manual.require_confirmation_for_destructive is True
    assert full_auto.slug == "full_auto"
    assert full_auto.require_confirmation_for_destructive is False


def test_orchestration_config_uses_assisted_profile_from_env(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_EXECUTION_MODE", "assisted")
    monkeypatch.setenv("AI_BRIDGE_CONFIRMATION_POLICY", "assisted")
    monkeypatch.setenv("AI_BRIDGE_AUTO_APPROVE", "true")
    monkeypatch.setenv("AI_BRIDGE_NON_INTERACTIVE", "false")

    config = OrchestrationConfig.from_env()

    assert config.execution_mode == "assisted"
    assert config.auto_approve_safe_tasks is True
    assert config.should_ask_confirmation({"type": "code", "risk_level": "low"}) is False
    assert config.should_ask_confirmation({"action": "database_delete"}) is True
    assert config.should_ask_confirmation({"action": "production_deploy"}) is True
