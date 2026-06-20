from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from core.core.env_loader import load_env_file
from core.core.mistral_model_registry import MistralModelRegistry

logger = logging.getLogger("MistralManager")


class MistralManager:
    def __init__(self, *, api_key: str | None = None) -> None:
        load_env_file()
        load_env_file(".env.bridge", override=True)
        self.api_key = api_key or os.getenv("MISTRAL_API_KEY")
        self.base_url = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").rstrip("/")
        self.timeout = self._read_timeout()
        self.registry = MistralModelRegistry()

    @staticmethod
    def _read_timeout() -> float:
        raw = os.getenv("MISTRAL_PROBE_TIMEOUT_SEC", os.getenv("AI_BRIDGE_PROVIDER_PROBE_TIMEOUT_SEC", "10")).strip()
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 10.0

    def _get_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def probe_models(self) -> dict[str, Any]:
        if not self.api_key:
            cached = self.registry.get_models(force_refresh=False)
            return {"ok": False, "status_code": None, "models": cached, "error": "missing_api_key", "inventory_source": "cache" if cached else "live"}
        try:
            import logging
            logging.getLogger("httpx").setLevel(logging.WARNING)

            response = httpx.get(f"{self.base_url}/models", headers=self._get_headers(), timeout=self.timeout)
            models: list[str] = []
            if response.status_code == 200:
                data = response.json().get("data", [])
                models = [str(model.get("id", "")).strip() for model in data if str(model.get("id", "")).strip()]
                filtered = self.registry._dedupe([model for model in models if self.registry._is_text_model(model)])
                if filtered:
                    self.registry._save_cache(filtered)
                    models = filtered
            return {
                "ok": response.status_code == 200,
                "status_code": response.status_code,
                "models": models,
                "error": None if response.status_code == 200 else response.text[:500],
                "inventory_source": "live" if response.status_code == 200 else "live_error",
            }
        except Exception as exc:
            cached = self.registry.get_models(force_refresh=False)
            return {
                "ok": False,
                "status_code": None,
                "models": cached,
                "error": str(exc),
                "inventory_source": "cache" if cached else "live_error",
            }

    def is_ready(self) -> bool:
        return self.probe_models().get("ok") is True

    def list_models(self) -> list[str]:
        probe = self.probe_models()
        models = [str(item).strip() for item in probe.get("models", []) if str(item).strip()]
        if models:
            return models
        return self.registry.get_models(force_refresh=False)

    def status(self) -> dict[str, Any]:
        probe = self.probe_models()
        return {
            "ready": probe.get("ok") is True,
            "models": probe.get("models", []),
            "api_probe": probe,
            "inventory_source": probe.get("inventory_source") or self.registry.diagnostics().get("source") or "live",
            "registry": self.registry.diagnostics(),
        }
