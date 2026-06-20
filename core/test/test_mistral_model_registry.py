from __future__ import annotations

import json

from core.core.mistral_model_registry import MistralModelRegistry


def test_mistral_registry_uses_cached_models(tmp_path, monkeypatch):
    cache = tmp_path / "mistral_models.json"
    cache.write_text(json.dumps({"ts": 4102444800, "models": ["mistral-large-latest", "mistral-embed", "codestral-latest"]}), encoding="utf-8")
    monkeypatch.setenv("MISTRAL_MODELS_CACHE_PATH", str(cache))
    monkeypatch.setenv("MISTRAL_MODELS_CACHE_TTL_SEC", "999999999")

    models = MistralModelRegistry().get_models()

    assert models == ["mistral-large-latest", "codestral-latest"]


def test_mistral_registry_falls_back_to_cache_when_live_fetch_fails(tmp_path, monkeypatch):
    cache = tmp_path / "mistral_models.json"
    cache.write_text(json.dumps({"ts": 4102444800, "models": ["devstral-latest", "mistral-medium-latest"]}), encoding="utf-8")
    monkeypatch.setenv("MISTRAL_MODELS_CACHE_PATH", str(cache))
    monkeypatch.setenv("MISTRAL_MODELS_CACHE_TTL_SEC", "999999999")
    monkeypatch.setattr(MistralModelRegistry, "_fetch_live", lambda self: (_ for _ in ()).throw(RuntimeError("live fetch failed")))

    models = MistralModelRegistry().get_models(force_refresh=True)

    assert models == ["devstral-latest", "mistral-medium-latest"]


def test_mistral_registry_catalog_groups_models(tmp_path, monkeypatch):
    cache = tmp_path / "mistral_models.json"
    cache.write_text(json.dumps({"ts": 4102444800, "models": ["mistral-large-latest", "mistral-medium-latest", "codestral-latest", "devstral-latest", "magistral-medium-latest"]}), encoding="utf-8")
    monkeypatch.setenv("MISTRAL_MODELS_CACHE_PATH", str(cache))
    monkeypatch.setenv("MISTRAL_MODELS_CACHE_TTL_SEC", "999999999")

    catalog = MistralModelRegistry().get_catalog()

    assert "mistral-large-latest" in catalog.large
    assert "mistral-medium-latest" in catalog.medium
    assert "codestral-latest" in catalog.codestral
    assert "devstral-latest" in catalog.devstral
    assert "magistral-medium-latest" in catalog.magistral
