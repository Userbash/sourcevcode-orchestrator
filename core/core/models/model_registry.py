from __future__ import annotations

import json
import os
from pathlib import Path


def _load_generated_models() -> list[str]:
    full_cache = Path(os.getenv("OPENAI_MODELS_FULL_CACHE_PATH", "core/.cache/openai_models_full.json"))
    try:
        payload = json.loads(full_cache.read_text(encoding="utf-8"))
    except Exception:
        return []
    models = payload.get("models") if isinstance(payload, dict) else []
    return [str(item).strip() for item in models if str(item).strip()] if isinstance(models, list) else []


def load_model_registry() -> list[str]:
    base = ["local-small", "mistral-large-latest", "gemini-1.5-pro"]
    dynamic = _load_generated_models()
    seen: set[str] = set()
    rows: list[str] = []
    for model in [*base, *dynamic]:
        if model not in seen:
            rows.append(model)
            seen.add(model)
    return rows


MODEL_REGISTRY = load_model_registry()
