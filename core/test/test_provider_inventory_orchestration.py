from __future__ import annotations

from core.core.orchestration_config import OrchestrationConfig


def test_orchestration_config_reads_provider_inventory_interval(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_PROVIDER_INVENTORY_REFRESH_INTERVAL_SEC", "777")

    config = OrchestrationConfig.from_env()

    assert config.provider_inventory_refresh_interval_sec == 777
    assert config.as_dict()["provider_inventory_refresh_interval_sec"] == 777
