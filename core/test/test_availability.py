from __future__ import annotations

import socket
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from core.core.availability import ModelAvailability, ProviderHealth, ProviderStatus


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _ok_socket(*_args, **_kwargs):
    return _FakeSocket()


def test_availability_init() -> None:
    avail = ModelAvailability()
    assert avail is not None


@patch("core.core.availability.shared_antigravity_snapshot")
@patch("socket.create_connection", side_effect=_ok_socket)
def test_check_antigravity_success(mock_snapshot: MagicMock, _mock_socket: MagicMock) -> None:
    mock_snapshot.return_value = {
        "ready": True,
        "ok": True,
        "models": ["model1", "model2"],
        "models_probe": {"ok": True},
        "generation_probe": {"ok": True},
        "auth_probe": {"ok": True},
        "api_probe": {},
        "auth_mode": "agy_oauth",
        "status": "ready",
    }

    avail = ModelAvailability()
    health = avail.check_antigravity()

    assert health.provider == "antigravity"
    assert health.status == ProviderStatus.HEALTHY
    assert health.latency_ms >= 0


def test_check_mistral_auth_missing_skips_tcp_probe(monkeypatch) -> None:
    avail = ModelAvailability()
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    with patch("socket.create_connection") as tcp_probe:
        health = avail.check_mistral()

    tcp_probe.assert_not_called()
    assert health.status == ProviderStatus.AUTH_FAILED
    assert health.error == "mistral_api_key_missing"


def test_check_openai_placeholder_key_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "example_openai_key")
    monkeypatch.delenv("CODEX_SALE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_SALE_BASE_URL", raising=False)
    monkeypatch.setattr("core.core.availability.load_env_file", lambda *args, **kwargs: None)
    avail = ModelAvailability()

    with patch("socket.create_connection") as tcp_probe:
        health = avail.check_openai()

    tcp_probe.assert_not_called()
    assert health.status == ProviderStatus.AUTH_FAILED
    assert health.error == "openai_api_key_placeholder"
    assert health.diagnostics["credential"]["placeholder"] is True


def test_is_provider_ready_cache() -> None:
    avail = ModelAvailability()
    health_ok = ProviderHealth("antigravity", ProviderStatus.HEALTHY, 10.0, datetime.now(UTC))

    avail._health_cache["antigravity"] = health_ok
    assert avail.is_provider_ready("antigravity") is True


def test_check_antigravity_tcp_timeout_blocks_live_probe() -> None:
    with patch("socket.create_connection", side_effect=socket.timeout("timed out")):
        avail = ModelAvailability()
        health = avail.check_antigravity(live=True)

    assert health.status == ProviderStatus.TIMEOUT
    assert health.error == "tcp_probe_failed"
    assert health.diagnostics["tcp"]["ok"] is False


def test_record_failure_updates_provider_cache() -> None:
    avail = ModelAvailability()
    health = avail.record_failure("google", "tcp_timeout", "connection timed out")

    assert health.provider == "antigravity"
    assert health.status == ProviderStatus.TIMEOUT
    assert avail.is_provider_ready("antigravity") is False
    assert health.diagnostics["error_type"] == "tcp_timeout"


def test_openai_tcp_targets_follow_custom_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://codex.sale/v1")
    monkeypatch.delenv("OPENAI_TCP_PROBE_HOSTS", raising=False)

    assert ModelAvailability._tcp_targets("openai") == [("codex.sale", 443)]


@patch("core.core.availability.build_mimo_runtime_status")
def test_check_mimo_healthy_from_runtime_snapshot(mock_mimo: MagicMock) -> None:
    mock_mimo.return_value = {
        "ready": True,
        "cli_available": True,
        "report_present": True,
        "usable_count": 24,
        "failed_count": 0,
        "usable_models_sample": ["mimo/mimo-auto"],
        "failed_models_sample": [],
        "auth_categories": {},
        "provider_breakdown": {"mimo": {"total": 24, "ok": 24, "failed": 0}},
    }

    avail = ModelAvailability()
    health = avail.check_mimo()

    assert health.provider == "mimo"
    assert health.status == ProviderStatus.HEALTHY
    assert health.diagnostics["snapshot"]["usable_count"] == 24


@patch("core.core.availability.build_mimo_runtime_status")
def test_check_mimo_auth_failed_from_runtime_snapshot(mock_mimo: MagicMock) -> None:
    mock_mimo.return_value = {
        "ready": False,
        "cli_available": True,
        "report_present": True,
        "usable_count": 0,
        "failed_count": 12,
        "usable_models_sample": [],
        "failed_models_sample": [{"model": "github-copilot/claude-haiku-4.5", "error": "pat not supported"}],
        "auth_categories": {"github_pat_not_supported": 12},
        "provider_breakdown": {"github-copilot": {"total": 12, "ok": 0, "failed": 12}},
    }

    avail = ModelAvailability()
    health = avail.check_mimo()

    assert health.provider == "mimo"
    assert health.status == ProviderStatus.AUTH_FAILED
    assert health.error == "mimo_auth_degraded"


def test_check_mistral_uses_snapshot_models_when_live_probe_fails(monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral_nonsecret_key_value_1234567890")
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: _FakeSocket())

    class _Manager:
        api_key = "mistral_nonsecret_key_value_1234567890"
        def status(self):
            return {"ready": False, "models": [], "api_probe": {"status_code": 500}, "inventory_source": "live_error", "registry": {"source": "cache"}}

    monkeypatch.setattr("core.core.availability.MistralManager", _Manager)
    avail = ModelAvailability()
    monkeypatch.setattr(avail.inventory, "provider_snapshot", lambda provider: {"provider": provider, "models": ["mistral-large-latest"], "source": "snapshot"})

    health = avail.check_mistral()

    assert health.status == ProviderStatus.DEGRADED
    assert health.diagnostics["models"] == ["mistral-large-latest"]
    assert health.diagnostics["inventory_source"] == "snapshot"


def test_check_openai_uses_snapshot_models_when_registry_empty(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: _FakeSocket())

    class _Registry:
        def get_models(self, force_refresh=False):
            return []
        def diagnostics(self):
            return {"ok": False, "error_type": "RuntimeError", "error_message": "empty"}

    monkeypatch.setattr("core.core.availability.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr(ModelAvailability, "_probe_openai_endpoint", staticmethod(lambda name, url, api_key: {"name": name, "url": url, "ok": True, "status_code": 200}))
    avail = ModelAvailability()
    monkeypatch.setattr(avail.inventory, "provider_snapshot", lambda provider: {"provider": provider, "models": ["gpt-5-mini"], "source": "snapshot"})

    health = avail.check_openai()

    assert health.status == ProviderStatus.HEALTHY
    assert health.diagnostics["models"] == ["gpt-5-mini"]
    assert health.diagnostics["inventory_source"] == "snapshot"



def test_check_openai_marks_auth_failed_when_models_endpoint_returns_401(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: _FakeSocket())

    class _Registry:
        def get_models(self, force_refresh=False):
            return []
        def diagnostics(self):
            return {"ok": False, "error_type": "auth_fail", "error_message": "auth_status_401", "status_code": 401, "endpoint": "https://codex.sale/v1/models", "source": "live"}

    monkeypatch.setattr("core.core.availability.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr(ModelAvailability, "_probe_openai_endpoint", staticmethod(lambda name, url, api_key: {"name": name, "url": url, "ok": False, "status_code": 401, "error_type": "auth_fail"} if name == "models" else {"name": name, "url": url, "ok": True, "status_code": 405}))

    health = ModelAvailability().check_openai()

    assert health.status == ProviderStatus.AUTH_FAILED
    assert health.error == "openai_auth_failed"


def test_check_openai_degrades_when_non_models_endpoints_are_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: _FakeSocket())

    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.5"]
        def diagnostics(self):
            return {"ok": True, "status_code": 200, "endpoint": "https://codex.sale/v1/models", "source": "live"}

    monkeypatch.setattr("core.core.availability.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr(ModelAvailability, "_probe_openai_endpoint", staticmethod(lambda name, url, api_key: {"name": name, "url": url, "ok": True, "status_code": 200} if name == "models" else {"name": name, "url": url, "ok": False, "status_code": 502, "error_type": "endpoint_unavailable"}))

    health = ModelAvailability().check_openai()

    assert health.status == ProviderStatus.DEGRADED
    assert "messages" in health.diagnostics["endpoint_statuses"]


def test_check_openai_exposes_endpoint_manifest(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai_usable_key_value_1234567890")
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: _FakeSocket())

    class _Registry:
        def get_models(self, force_refresh=False):
            return ["gpt-5.5"]
        def diagnostics(self):
            return {"ok": True, "status_code": 200, "endpoint": "https://codex.sale/v1/models", "source": "live"}

    monkeypatch.setattr("core.core.availability.OpenAIModelRegistry", _Registry)
    monkeypatch.setattr(ModelAvailability, "_probe_openai_endpoint", staticmethod(lambda name, url, api_key: {"name": name, "url": url, "ok": True, "status_code": 200}))

    health = ModelAvailability().check_openai()

    assert health.diagnostics["endpoint_manifest"]["endpoints"]["codex"].endswith("/backend-api/codex")
