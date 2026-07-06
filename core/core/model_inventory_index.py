from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ModelInventoryIndex:
    path: Path
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_provider: dict[str, list[str]] = field(default_factory=dict)
    updated_at: int = 0

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        return str(provider or "").strip().lower()

    @staticmethod
    def _normalize_model(model_name: str) -> str:
        return str(model_name or "").strip()

    def rebuild(self, providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
        by_model: dict[str, dict[str, Any]] = {}
        by_provider: dict[str, list[str]] = {}
        for provider_name, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            provider = self._normalize_provider(entry.get("provider") or provider_name)
            diagnostics = entry.get("diagnostics") if isinstance(entry.get("diagnostics"), dict) else {}
            resident = set(str(item).strip() for item in diagnostics.get("resident_models", []) if str(item).strip())
            models = [self._normalize_model(item) for item in (entry.get("models") or []) if self._normalize_model(item)]
            if not models:
                continue
            by_provider[provider] = list(models)
            for model_name in models:
                row = {
                    "provider": provider,
                    "model_name": model_name,
                    "source": entry.get("source"),
                    "ok": bool(entry.get("ok")),
                    "status_code": entry.get("status_code"),
                    "error": entry.get("error"),
                    "fetched_at": entry.get("fetched_at"),
                    "resident": model_name in resident,
                    "diagnostics": diagnostics,
                }
                by_model[model_name] = row
        self.by_model = by_model
        self.by_provider = by_provider
        self.updated_at = int(time.time())
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "total_models": len(self.by_model),
            "provider_counts": {provider: len(models) for provider, models in self.by_provider.items()},
            "by_model": self.by_model,
            "by_provider": self.by_provider,
        }

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.snapshot(), ensure_ascii=True, indent=2), encoding="utf-8")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.snapshot()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self.snapshot()
        if not isinstance(payload, dict):
            return self.snapshot()
        self.updated_at = int(payload.get("updated_at") or 0)
        self.by_model = payload.get("by_model") if isinstance(payload.get("by_model"), dict) else {}
        self.by_provider = payload.get("by_provider") if isinstance(payload.get("by_provider"), dict) else {}
        return self.snapshot()

    def find_model(self, model_name: str) -> dict[str, Any] | None:
        return self.by_model.get(self._normalize_model(model_name))

    def provider_models(self, provider: str) -> list[str]:
        return list(self.by_provider.get(self._normalize_provider(provider), []))
