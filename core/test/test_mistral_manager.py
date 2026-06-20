import pytest
import os
from unittest.mock import MagicMock, patch
from core.core.integrations.mistral_manager import MistralManager

def test_mistral_manager_checks_readiness_success():
    with patch("httpx.get") as mock_get:
        # Имитируем успешный ответ API Mistral
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"data": [{"id": "mistral-tiny"}, {"id": "mistral-small"}]})
        
        manager = MistralManager(api_key="mistral_nonsecret_key_value_1234567890")
        
        # Test readiness check
        assert manager.is_ready() is True
        
        # Test models list
        models = manager.list_models()
        assert "mistral-tiny" in models
        assert "mistral-small" in models
        
def test_mistral_manager_handles_auth_failure():
    with patch("httpx.get") as mock_get:
        # Имитируем ошибку авторизации
        mock_get.return_value = MagicMock(status_code=401)
        
        manager = MistralManager(api_key="mistral_invalid_key_value_1234567890")
        
        # Test readiness check
        assert manager.is_ready() is False


def test_mistral_manager_uses_cached_registry_when_live_probe_fails(monkeypatch):
    manager = MistralManager(api_key="mistral_nonsecret_key_value_1234567890")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")))
    monkeypatch.setattr(manager.registry, "get_models", lambda force_refresh=False: ["mistral-large-latest", "codestral-latest"])

    probe = manager.probe_models()

    assert probe["ok"] is False
    assert probe["models"] == ["mistral-large-latest", "codestral-latest"]
    assert probe["inventory_source"] == "cache"
