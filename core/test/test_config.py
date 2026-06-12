import os
import yaml
import pytest
from pathlib import Path

from core.core.config import ConfigLoader
from core.core.settings import BridgeSettings

def test_config_load_defaults():
    """
    TDD: Test that the loader returns default values if no file is provided.
    """
    loader = ConfigLoader()
    config = loader.get_config()
    
    assert config["project"]["name"] == "ai-bridge"
    assert config["orchestrator"]["max_parallel_tasks"] == 5
    assert config["orchestration"]["enabled_by_default"] is True

def test_config_load_from_yaml(tmp_path):
    """
    TDD: Test that the loader correctly overrides values from a YAML file.
    """
    d = tmp_path / "config"
    d.mkdir()
    config_file = d / "test_config.yaml"
    
    test_data = {
        "project": {"name": "custom-bridge"},
        "orchestrator": {"max_parallel_tasks": 10}
    }
    
    with open(config_file, "w") as f:
        yaml.dump(test_data, f)
    
    loader = ConfigLoader(config_path=str(config_file))
    config = loader.get_config()
    
    assert config["project"]["name"] == "custom-bridge"
    assert config["orchestrator"]["max_parallel_tasks"] == 10
    # Check that non-overridden defaults remain
    assert config["orchestration"]["enabled_by_default"] is True

def test_config_env_override():
    """
    TDD: Test that environment variables override YAML and defaults.
    """
    os.environ["AI_BRIDGE_PROJECT_NAME"] = "env-bridge"
    os.environ["AI_BRIDGE_ORCHESTRATOR_MAX_PARALLEL_TASKS"] = "20"
    
    loader = ConfigLoader()
    config = loader.get_config()
    
    assert config["project"]["name"] == "env-bridge"
    assert config["orchestrator"]["max_parallel_tasks"] == 20
    
    # Cleanup
    del os.environ["AI_BRIDGE_PROJECT_NAME"]
    del os.environ["AI_BRIDGE_ORCHESTRATOR_MAX_PARALLEL_TASKS"]


def test_config_example_loads_into_typed_settings():
    settings = BridgeSettings.load(Path("core/CONFIG.example.yaml"))

    assert settings.project.name == "ai-bridge"
    assert settings.orchestrator.retry_limit == 3
    assert settings.load_balancing.strategy == "weighted_score"
    assert settings.agents[0].capabilities == ["code", "fix", "refactor"]


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_ORCHESTRATOR_MAX_PARALLEL_TASKS", "12")

    settings = BridgeSettings.load(Path("core/CONFIG.example.yaml"))

    assert settings.orchestrator.max_parallel_tasks == 12


from core.core.orchestration_config import OrchestrationConfig


def test_orchestration_config_exposes_trained_memory_controls():
    config = OrchestrationConfig.from_env()
    assert isinstance(config.trained_memory_quality_thresholds_by_task, dict)
    assert "plan" in config.trained_memory_quality_thresholds_by_task
    assert config.high_risk_trained_memory_enabled is False


def test_orchestration_config_cli_can_enable_high_risk_trained_memory():
    config = OrchestrationConfig()
    config.apply_cli_flags(high_risk_trained_memory=True)
    assert config.high_risk_trained_memory_enabled is True


from core.core.orchestration_config import OrchestrationConfig


def test_orchestration_config_exposes_kpi_thresholds():
    config = OrchestrationConfig()
    assert config.kpi_thresholds_by_task["plan"] >= 0.7
    assert config.kpi_routing_floor_by_task["review"] >= 0.7
    assert config.trained_memory_degrade_ttl_sec >= 120
