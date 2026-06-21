from __future__ import annotations

import json
import os
from pathlib import Path


def _default_capabilities() -> dict[str, list[str]]:
    return {
        "local-small": ["code", "test"],
        "mistral-large-latest": ["code", "review", "reasoning"],
        "gemini-1.5-pro": ["research", "long_context"],
    }


def _load_generated_capabilities() -> dict[str, list[str]]:
    path = Path(os.getenv("OPENAI_ORCHESTRATOR_TEMPLATES_PATH", "core/mimo/profiles/generated/openai_compatible/orchestrator_templates.json"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    roles = payload.get("roles") if isinstance(payload, dict) else {}
    if not isinstance(roles, dict):
        return {}
    capabilities: dict[str, set[str]] = {}
    for rows in roles.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model_name") or "").strip()
            if not model_name:
                continue
            caps = capabilities.setdefault(model_name, set())
            for item in row.get("preferred_task_types", []):
                caps.add(str(item))
            for strength in row.get("strengths", []):
                caps.add(str(strength))
    return {model: sorted(values) for model, values in capabilities.items()}


def load_model_capabilities() -> dict[str, list[str]]:
    merged = _default_capabilities()
    for model, values in _load_generated_capabilities().items():
        existing = set(merged.get(model, []))
        merged[model] = sorted(existing.union(values))
    return merged


MODEL_CAPABILITIES = load_model_capabilities()
