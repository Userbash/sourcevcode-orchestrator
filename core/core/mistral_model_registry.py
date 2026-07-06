from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(slots=True)
class MistralModelCatalog:
    all_models: list[str]
    small: list[str]
    medium: list[str]
    large: list[str]
    codestral: list[str]
    devstral: list[str]
    magistral: list[str]
    others: list[str]


@dataclass(slots=True)
class MistralRegistryDiagnostics:
    ok: bool
    error_type: str | None = None
    error_message: str | None = None
    status_code: int | None = None
    source: str | None = None


class MistralModelRegistry:
    @staticmethod
    def _resolve_base_url() -> str:
        configured = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").rstrip("/")
        if "host.containers.internal:8012" in configured or "127.0.0.1:8012" in configured or "localhost:8012" in configured:
            return "https://api.mistral.ai/v1"
        return configured or "https://api.mistral.ai/v1"

    def __init__(self) -> None:
        self.cache_path = Path(os.getenv("MISTRAL_MODELS_CACHE_PATH", "core/.cache/mistral_models.json"))
        self.ttl_sec = int(os.getenv("MISTRAL_MODELS_CACHE_TTL_SEC", "21600"))
        self.base_url = self._resolve_base_url()
        self.timeout = self._read_timeout()
        self._last_diagnostics = MistralRegistryDiagnostics(ok=True, source="cache")

    @staticmethod
    def _read_timeout() -> float:
        raw = os.getenv("MISTRAL_PROBE_TIMEOUT_SEC", os.getenv("AI_BRIDGE_PROVIDER_PROBE_TIMEOUT_SEC", "10")).strip()
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 10.0

    @staticmethod
    def _api_key() -> str:
        return os.getenv("MISTRAL_API_KEY", "").strip()

    @staticmethod
    def _dedupe(models: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for model in models:
            if model in seen:
                continue
            seen.add(model)
            deduped.append(model)
        return deduped

    @staticmethod
    def _is_text_model(model_id: str) -> bool:
        lowered = str(model_id or "").strip().lower()
        if not lowered:
            return False
        if any(token in lowered for token in ("embed", "moderation", "ocr", "tts", "transcribe", "realtime")):
            return False
        return any(token in lowered for token in ("mistral", "codestral", "devstral", "magistral"))

    def _fetch_live(self) -> list[str]:
        key = self._api_key()
        if not key:
            self._last_diagnostics = MistralRegistryDiagnostics(
                ok=False,
                error_type="missing_api_key",
                error_message="MISTRAL_API_KEY is not set",
                source="live",
            )
            return []
        try:
            response = httpx.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=self.timeout,
            )
        except Exception as exc:
            self._last_diagnostics = MistralRegistryDiagnostics(
                ok=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
                source="live",
            )
            return []

        if response.status_code != 200:
            self._last_diagnostics = MistralRegistryDiagnostics(
                ok=False,
                error_type="http_error",
                error_message=response.text[:500],
                status_code=response.status_code,
                source="live",
            )
            return []

        data = response.json().get("data", [])
        models = [
            str(item.get("id", "")).strip()
            for item in data
            if self._is_text_model(str(item.get("id", "")).strip())
        ]
        models = self._dedupe(models)
        self._last_diagnostics = MistralRegistryDiagnostics(ok=True, status_code=200, source="live")
        return models

    def _load_cache(self) -> list[str]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            ts = int(payload.get("ts", 0))
            if int(time.time()) - ts > self.ttl_sec:
                return []
            return [
                str(item)
                for item in payload.get("models", [])
                if self._is_text_model(str(item))
            ]
        except Exception:
            return []

    def _save_cache(self, models: list[str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": int(time.time()), "models": self._dedupe(models)}
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

    def get_models(self, force_refresh: bool = False) -> list[str]:
        if not force_refresh:
            cached = self._load_cache()
            if cached:
                self._last_diagnostics = MistralRegistryDiagnostics(ok=True, source="cache")
                return cached
        try:
            live = self._fetch_live()
        except Exception as exc:
            self._last_diagnostics = MistralRegistryDiagnostics(ok=False, error_type=type(exc).__name__, error_message=str(exc), source="live")
            live = []
        if live:
            self._save_cache(live)
            return live
        cached = self._load_cache()
        if cached:
            self._last_diagnostics = MistralRegistryDiagnostics(ok=True, source="cache")
        return cached

    def diagnostics(self) -> dict[str, str | int | bool | None]:
        return {
            "ok": self._last_diagnostics.ok,
            "error_type": self._last_diagnostics.error_type,
            "error_message": self._last_diagnostics.error_message,
            "status_code": self._last_diagnostics.status_code,
            "source": self._last_diagnostics.source,
        }

    def get_catalog(self, force_refresh: bool = False) -> MistralModelCatalog:
        models = self.get_models(force_refresh=force_refresh)
        lower = {model: model.lower() for model in models}
        small = [model for model, value in lower.items() if "small" in value]
        medium = [model for model, value in lower.items() if "medium" in value]
        large = [model for model, value in lower.items() if "large" in value]
        codestral = [model for model, value in lower.items() if "codestral" in value]
        devstral = [model for model, value in lower.items() if "devstral" in value]
        magistral = [model for model, value in lower.items() if "magistral" in value]
        classified = set(small + medium + large + codestral + devstral + magistral)
        others = [model for model in models if model not in classified]
        return MistralModelCatalog(models, small, medium, large, codestral, devstral, magistral, others)
