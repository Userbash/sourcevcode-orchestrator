from __future__ import annotations

import json

from core.core.antigravity_model_registry import AntigravityModelRegistry


def test_antigravity_registry_falls_back_to_cache_when_live_fetch_raises(tmp_path, monkeypatch):
    cache = tmp_path / "antigravity_models.json"
    cache.write_text(json.dumps({"ts": 4_102_444_800, "models": ["antigravity-flash", "antigravity-pro"]}), encoding="utf-8")
    monkeypatch.setenv("ANTIGRAVITY_MODELS_CACHE_PATH", str(cache))
    monkeypatch.setenv("ANTIGRAVITY_MODELS_CACHE_TTL_SEC", "999999999")
    monkeypatch.setattr(AntigravityModelRegistry, "_fetch_live", lambda self: (_ for _ in ()).throw(RuntimeError("live fetch failed")))

    registry = AntigravityModelRegistry()

    assert registry.get_models(force_refresh=True) == ["antigravity-flash", "antigravity-pro"]


def test_antigravity_registry_returns_empty_when_no_cache_and_live_fetch_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_MODELS_CACHE_PATH", str(tmp_path / "antigravity_models.json"))
    monkeypatch.setattr(AntigravityModelRegistry, "_fetch_live", lambda self: (_ for _ in ()).throw(RuntimeError("live fetch failed")))

    registry = AntigravityModelRegistry()

    assert registry.get_models(force_refresh=True) == []


def test_antigravity_registry_filters_invalid_cached_model_names(tmp_path, monkeypatch):
    cache = tmp_path / "antigravity_models.json"
    cache.write_text(json.dumps({"ts": 4_102_444_800, "models": ["authentication-required.-error:-authentication-timed-out.", "antigravity-flash"]}), encoding="utf-8")
    monkeypatch.setenv("ANTIGRAVITY_MODELS_CACHE_PATH", str(cache))
    monkeypatch.setenv("ANTIGRAVITY_MODELS_CACHE_TTL_SEC", "999999999")

    registry = AntigravityModelRegistry()

    assert registry.get_models(force_refresh=False) == ["antigravity-flash"]


class _HTTPResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.content = b"{}"
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


def test_antigravity_registry_fetches_models_from_http_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_API_KEY", "test-key")
    monkeypatch.setenv("ANTIGRAVITY_MODELS_CACHE_PATH", str(tmp_path / "antigravity_models.json"))

    def fake_get(url, headers=None, timeout=None):
        assert headers == {"Content-Type": "application/json", "api-key": "test-key", "Authorization": "Bearer test-key"}
        assert url.endswith("/models")
        return _HTTPResponse({
            "models": [
                {"name": "models/antigravity-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/antigravity-pro", "supportedGenerationMethods": ["generateContent"]},
            ]
        })

    monkeypatch.setattr("core.core.antigravity_model_registry.httpx.get", fake_get)

    registry = AntigravityModelRegistry()

    assert registry.get_models(force_refresh=True) == ["antigravity-flash", "antigravity-pro"]
