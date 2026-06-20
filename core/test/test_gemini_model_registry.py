from __future__ import annotations

import json

from core.core.gemini_model_registry import AntigravityModelRegistry


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
