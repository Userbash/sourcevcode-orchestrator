from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .openai_provider import resolve_openai_provider_config


@dataclass(slots=True)
class OpenAIModelCatalog:
    all_models: list[str]
    nano: list[str]
    mini: list[str]
    standard: list[str]
    codex: list[str]
    pro: list[str]
    reasoning: list[str]


@dataclass(slots=True)
class OpenAIRegistryDiagnostics:
    ok: bool
    error_type: str | None = None
    error_message: str | None = None
    status_code: int | None = None
    endpoint: str | None = None
    source: str = "live"


class OpenAIModelRegistry:
    def __init__(self) -> None:
        self.cache_path = Path(os.getenv("OPENAI_MODELS_CACHE_PATH", "core/.cache/openai_models.json"))
        self.ttl_sec = int(os.getenv("OPENAI_MODELS_CACHE_TTL_SEC", "21600"))
        self._last_diagnostics = OpenAIRegistryDiagnostics(ok=True)

    @staticmethod
    def _api_key() -> str:
        return str(resolve_openai_provider_config().api_key or "").strip()

    @staticmethod
    def _is_text_model(model_id: str) -> bool:
        lowered = model_id.lower()
        if any(token in lowered for token in ("embedding", "moderation", "tts", "whisper", "image", "sora", "dall", "realtime", "audio", "transcribe")):
            return False
        return lowered.startswith(("gpt-", "o", "codex")) or "codex" in lowered or "claude" in lowered or "deepseek" in lowered or "qwen" in lowered or "kimi" in lowered or "glm" in lowered or "mimo" in lowered

    @staticmethod
    def _classify_status_code(status_code: int) -> tuple[str, str]:
        if 300 <= status_code < 400:
            return "redirect_status", f"unexpected_redirect_{status_code}"
        if status_code in {401, 403}:
            return "auth_fail", f"auth_status_{status_code}"
        if status_code == 404:
            return "endpoint_not_found", "models_endpoint_not_found"
        if status_code == 429:
            return "quota_exhaustion", "models_endpoint_rate_limited"
        if 500 <= status_code < 600:
            return "endpoint_unavailable", f"models_endpoint_unavailable_{status_code}"
        return "http_error", f"models_endpoint_http_{status_code}"

    def _fetch_live(self) -> list[str]:
        cfg = resolve_openai_provider_config()
        key = str(cfg.api_key or "").strip()
        endpoint = str(cfg.models_endpoint or "").strip()
        if not key:
            self._last_diagnostics = OpenAIRegistryDiagnostics(
                ok=False,
                error_type="missing_api_key",
                error_message="OpenAI API key is not configured in env or discovery artifact",
                endpoint=endpoint,
            )
            return []
        if not endpoint:
            self._last_diagnostics = OpenAIRegistryDiagnostics(ok=False, error_type="missing_models_endpoint", error_message="OpenAI models endpoint is not configured", endpoint=None)
            return []
        try:
            response = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {key}"},
                timeout=10.0,
                allow_redirects=False,
            )
        except Exception as exc:
            from .external_ai_bridge import ExternalAIBridge

            classified = ExternalAIBridge.classify_error(str(exc))
            self._last_diagnostics = OpenAIRegistryDiagnostics(
                ok=False,
                error_type=classified,
                error_message=str(exc),
                endpoint=endpoint,
                source="live",
            )
            return []

        if response.status_code != 200:
            error_type, error_message = self._classify_status_code(response.status_code)
            self._last_diagnostics = OpenAIRegistryDiagnostics(
                ok=False,
                error_type=error_type,
                error_message=error_message,
                status_code=response.status_code,
                endpoint=endpoint,
                source="live",
            )
            return []

        try:
            payload = response.json() if response.content else {}
        except Exception as exc:
            self._last_diagnostics = OpenAIRegistryDiagnostics(
                ok=False,
                error_type="invalid_json",
                error_message=str(exc),
                status_code=response.status_code,
                endpoint=endpoint,
                source="live",
            )
            return []

        out: list[str] = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            model_id = str((item or {}).get("id") or "").strip()
            if model_id and self._is_text_model(model_id):
                out.append(model_id)
        self._last_diagnostics = OpenAIRegistryDiagnostics(ok=True, status_code=response.status_code, endpoint=endpoint, source="live")
        return self._dedupe(out)

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

    def _load_cache(self) -> list[str]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            ts = int(payload.get("ts", 0))
            if int(time.time()) - ts > self.ttl_sec:
                return []
            return [str(item) for item in payload.get("models", []) if str(item)]
        except Exception:
            return []

    def _save_cache(self, models: list[str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps({"ts": int(time.time()), "models": models}, ensure_ascii=True), encoding="utf-8")

    def get_models(self, force_refresh: bool = False) -> list[str]:
        if not force_refresh:
            cached = self._load_cache()
            if cached:
                self._last_diagnostics = OpenAIRegistryDiagnostics(ok=True, source="cache")
                return cached
        live = self._fetch_live()
        if live:
            self._save_cache(live)
            return live
        cached = self._load_cache()
        if cached:
            self._last_diagnostics = OpenAIRegistryDiagnostics(
                ok=True,
                error_type=self._last_diagnostics.error_type,
                error_message=self._last_diagnostics.error_message,
                status_code=self._last_diagnostics.status_code,
                endpoint=self._last_diagnostics.endpoint,
                source="cache_fallback",
            )
        return cached

    def diagnostics(self) -> dict[str, str | bool | int | None]:
        return {
            "ok": self._last_diagnostics.ok,
            "error_type": self._last_diagnostics.error_type,
            "error_message": self._last_diagnostics.error_message,
            "status_code": self._last_diagnostics.status_code,
            "endpoint": self._last_diagnostics.endpoint,
            "source": self._last_diagnostics.source,
        }

    def get_catalog(self, force_refresh: bool = False) -> OpenAIModelCatalog:
        models = self.get_models(force_refresh=force_refresh)
        lower = {model: model.lower() for model in models}
        nano = [model for model, value in lower.items() if "nano" in value]
        mini = [model for model, value in lower.items() if "mini" in value and "codex" not in value]
        codex = [model for model, value in lower.items() if "codex" in value]
        pro = [model for model, value in lower.items() if "pro" in value]
        reasoning = [model for model, value in lower.items() if value.startswith("o") or "reason" in value]
        standard = [model for model in models if model not in set(nano + mini + codex + pro)]
        return OpenAIModelCatalog(models, nano, mini, standard, codex, pro, reasoning)
