from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .antigravity_provider import antigravity_request_headers, resolve_antigravity_provider_config


@dataclass(slots=True)
class AntigravityModelCatalog:
    all_models: list[str]
    lite: list[str]
    flash: list[str]
    pro: list[str]
    thinking: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AntigravityRegistryDiagnostics:
    ok: bool
    error_type: str | None = None
    error_message: str | None = None
    status_code: int | None = None
    source: str | None = None


class AntigravityModelRegistry:
    def __init__(self) -> None:
        cfg = resolve_antigravity_provider_config()
        self.api_base = cfg.models_endpoint
        self.cache_path = Path(os.getenv("ANTIGRAVITY_MODELS_CACHE_PATH", os.getenv("GEMINI_MODELS_CACHE_PATH", "core/.cache/antigravity_models.json")))
        self.ttl_sec = int(os.getenv("ANTIGRAVITY_MODELS_CACHE_TTL_SEC", os.getenv("GEMINI_MODELS_CACHE_TTL_SEC", "21600")))
        self.timeout = float(os.getenv("ANTIGRAVITY_MODELS_PROBE_TIMEOUT_SEC", os.getenv("GEMINI_MODELS_PROBE_TIMEOUT_SEC", os.getenv("AI_BRIDGE_PROVIDER_PROBE_TIMEOUT_SEC", "20"))))
        self._last_diagnostics = AntigravityRegistryDiagnostics(ok=True, source="cache")

    @staticmethod
    def _api_key() -> str:
        return os.getenv("ANTIGRAVITY_API_KEY", "").strip() or os.getenv("ANTIGRAVITY_API_TOKEN", "").strip() or os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()

    @staticmethod
    def _normalize_model_name(raw: str) -> str:
        return str(raw or "").strip().replace("models/", "").replace(" ", "-").lower()

    @classmethod
    def _looks_like_model_name(cls, raw: str) -> bool:
        name = cls._normalize_model_name(raw)
        if not name or len(name) > 128:
            return False
        if any(marker in name for marker in ("authentication", "permission_denied", "timed-out", "timed_out", "rate-limit", "resource_exhausted", "api_key", "forbidden", "unauthorized", "error:")):
            return False
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", name):
            return False
        return any(marker in name for marker in ("flash", "lite", "pro", "thinking", "gemini", "antigravity", "claude", "omni"))

    @classmethod
    def _filter_models(cls, models: list[str]) -> list[str]:
        seen: set[str] = set()
        filtered: list[str] = []
        for raw in models:
            name = cls._normalize_model_name(raw)
            if not cls._looks_like_model_name(name) or name in seen:
                continue
            seen.add(name)
            filtered.append(name)
        return filtered

    def _fetch_live(self) -> list[str]:
        key = self._api_key()
        if not key:
            self._last_diagnostics = AntigravityRegistryDiagnostics(ok=False, error_type="missing_api_key", error_message="ANTIGRAVITY_API_KEY is not set", source="live")
            return []
        try:
            response = httpx.get(self.api_base, headers=antigravity_request_headers(key), timeout=self.timeout)
        except Exception as exc:
            self._last_diagnostics = AntigravityRegistryDiagnostics(ok=False, error_type=type(exc).__name__, error_message=str(exc), source="live")
            return []
        if response.status_code != 200:
            self._last_diagnostics = AntigravityRegistryDiagnostics(ok=False, error_type="http_error", error_message=response.text[:500], status_code=response.status_code, source="live")
            return []
        payload = response.json() if response.content else {}
        rows = payload.get("models", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[str] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "").strip()
            if self._looks_like_model_name(name):
                models.append(self._normalize_model_name(name))
        models = self._filter_models(models)
        self._last_diagnostics = AntigravityRegistryDiagnostics(ok=True, status_code=200, source="live")
        return models

    def _load_cache(self) -> list[str]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            ts = int(payload.get("ts", 0))
            if int(time.time()) - ts > self.ttl_sec:
                return []
            return self._filter_models([str(x) for x in payload.get("models", []) if str(x)])
        except Exception:
            return []

    def _save_cache(self, models: list[str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({"ts": int(time.time()), "models": self._filter_models(models)}, ensure_ascii=True), encoding="utf-8")

    def get_models(self, force_refresh: bool = False) -> list[str]:
        cached = [] if force_refresh else self._load_cache()
        if cached:
            self._last_diagnostics = AntigravityRegistryDiagnostics(ok=True, source="cache")
            return cached
        try:
            live = self._fetch_live()
        except Exception as exc:
            self._last_diagnostics = AntigravityRegistryDiagnostics(ok=False, error_type=type(exc).__name__, error_message=str(exc), source="live")
            live = []
        if live:
            self._save_cache(live)
            return live
        cached = self._load_cache()
        if cached:
            self._last_diagnostics = AntigravityRegistryDiagnostics(ok=True, source="cache")
        return cached

    def diagnostics(self) -> dict[str, str | int | bool | None]:
        return {
            "ok": self._last_diagnostics.ok,
            "error_type": self._last_diagnostics.error_type,
            "error_message": self._last_diagnostics.error_message,
            "status_code": self._last_diagnostics.status_code,
            "source": self._last_diagnostics.source,
        }

    def get_catalog(self, force_refresh: bool = False) -> AntigravityModelCatalog:
        models = self.get_models(force_refresh=force_refresh)
        lower = {model: model.lower() for model in models}
        lite = [model for model, value in lower.items() if "lite" in value]
        flash = [model for model, value in lower.items() if "flash" in value and "lite" not in value]
        pro = [model for model, value in lower.items() if "pro" in value or ("claude-sonnet" in value and "thinking" not in value)]
        thinking = [model for model, value in lower.items() if "thinking" in value or "claude-opus" in value]
        return AntigravityModelCatalog(models, lite, flash, pro, thinking)


GeminiModelCatalog = AntigravityModelCatalog
GeminiModelRegistry = AntigravityModelRegistry
