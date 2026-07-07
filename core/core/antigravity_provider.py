from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import httpx

from .openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR, extract_provider_response_text, has_meaningful_request_payload, provider_response_has_assistant_content_or_tool_calls


@dataclass(slots=True)
class AntigravityProviderConfig:
    api_key: str
    base_url: str
    models_endpoint: str
    chat_completions_endpoint: str
    default_model: str
    auth_header_name: str


def _first_env(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name, "") or "").strip()
        if value:
            return value
    return ""


def _normalize_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _join_url(base: str, suffix: str) -> str:
    base_clean = _normalize_url(base)
    if not base_clean:
        return ""
    return f"{base_clean}/{suffix.lstrip('/')}"


def resolve_antigravity_provider_config() -> AntigravityProviderConfig:
    api_key = _first_env("ANTIGRAVITY_API_KEY", "ANTIGRAVITY_API_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    explicit_base_url = _normalize_url(_first_env("ANTIGRAVITY_BASE_URL", "ANTIGRAVITY_API_BASE_URL", "GEMINI_API_BASE_URL"))
    base_url = explicit_base_url or "https://generativelanguage.googleapis.com/v1beta/openai"
    models_endpoint = _normalize_url(_first_env("ANTIGRAVITY_MODELS_ENDPOINT", "ANTIGRAVITY_API_MODELS_ENDPOINT")) or _join_url(base_url, "models")
    chat_endpoint = _normalize_url(_first_env("ANTIGRAVITY_CHAT_COMPLETIONS_ENDPOINT", "ANTIGRAVITY_API_CHAT_COMPLETIONS_ENDPOINT")) or _join_url(base_url, "chat/completions")
    default_model = _first_env("ANTIGRAVITY_DEFAULT_MODEL", "GEMINI_DEFAULT_MODEL") or "gemini-2.5-flash-lite"
    auth_header_name = "api-key" if api_key else "api-key"
    return AntigravityProviderConfig(
        api_key=api_key,
        base_url=base_url,
        models_endpoint=models_endpoint,
        chat_completions_endpoint=chat_endpoint,
        default_model=default_model,
        auth_header_name=auth_header_name,
    )


def antigravity_request_headers(api_key: str) -> dict[str, str]:
    token = str(api_key or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["api-key"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def antigravity_endpoint_manifest(config: AntigravityProviderConfig | None = None) -> dict[str, object]:
    cfg = config or resolve_antigravity_provider_config()
    return {
        "provider": "antigravity",
        "base_url": cfg.base_url,
        "default_model": cfg.default_model,
        "endpoints": {
            "models": cfg.models_endpoint,
            "chat_completions": cfg.chat_completions_endpoint,
        },
        "endpoint_roles": {
            "inventory": "models",
            "chat": "chat_completions",
        },
    }


_LEGACY_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "antigravity-flash-lite": ("gemini-2.5-flash-lite", "gemini-2.0-flash-lite", "gemini-flash-lite-latest"),
    "antigravity-flash": ("gemini-2.5-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash"),
    "antigravity-pro": ("gemini-2.5-pro", "gemini-pro-latest", "antigravity-preview-05-2026"),
    "antigravity-thinking": ("gemini-2.5-pro", "gemini-3.1-pro-preview", "deep-research-pro-preview-12-2025"),
}


def resolve_antigravity_model_alias(model_name: str, available_models: list[str] | None = None) -> str:
    normalized = _normalize_model_name(model_name)
    if not normalized:
        normalized = _normalize_model_name(resolve_antigravity_provider_config().default_model)
    candidates = _LEGACY_MODEL_ALIASES.get(normalized)
    available = {_normalize_model_name(item) for item in (available_models or []) if _normalize_model_name(item)}
    if candidates:
        for candidate in candidates:
            candidate_name = _normalize_model_name(candidate)
            if not available or candidate_name in available:
                return candidate_name
    return normalized


def _normalize_model_name(raw: str) -> str:
    return str(raw or "").strip().replace("models/", "").replace(" ", "-").lower()


def _looks_like_model_name(raw: str) -> bool:
    name = _normalize_model_name(raw)
    if not name or len(name) > 128:
        return False
    if any(marker in name for marker in ("authentication", "permission_denied", "timed-out", "timed_out", "rate-limit", "resource_exhausted", "api_key", "forbidden", "unauthorized", "error:")):
        return False
    if not __import__("re").fullmatch(r"[a-z0-9][a-z0-9._/-]*", name):
        return False
    return any(marker in name for marker in ("flash", "lite", "pro", "thinking", "gemini", "antigravity", "claude", "omni"))


def _filter_models(models: list[str]) -> list[str]:
    seen: set[str] = set()
    filtered: list[str] = []
    for raw in models:
        name = _normalize_model_name(raw)
        if not _looks_like_model_name(name) or name in seen:
            continue
        seen.add(name)
        filtered.append(name)
    return filtered


def _cache_path() -> Path:
    return Path(os.getenv("ANTIGRAVITY_MODELS_CACHE_PATH", os.getenv("GEMINI_MODELS_CACHE_PATH", "core/.cache/antigravity_models.json")))


def _cache_ttl_sec() -> int:
    raw = str(os.getenv("ANTIGRAVITY_MODELS_CACHE_TTL_SEC", os.getenv("GEMINI_MODELS_CACHE_TTL_SEC", "21600")) or "21600").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 21600


def _load_cache() -> dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_cache(models: list[str], *, status_code: int | None = None, endpoint: str = "") -> None:
    payload = {
        "ts": int(time.time()),
        "models": _filter_models(models),
        "status_code": status_code,
        "endpoint": endpoint,
    }
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def fetch_antigravity_model_catalog(*, force_refresh: bool = False, timeout_sec: float = 20.0) -> dict[str, Any]:
    cfg = resolve_antigravity_provider_config()
    cached = _load_cache()
    cached_models = _filter_models([str(item) for item in cached.get("models", []) if str(item).strip()]) if cached else []
    cached_ts = int(cached.get("ts") or 0) if cached else 0
    if cached_models and not force_refresh and (time.time() - cached_ts) < _cache_ttl_sec():
        return {
            "ok": True,
            "source": "cache",
            "provider": "antigravity",
            "base_url": cfg.base_url,
            "endpoint": cfg.models_endpoint,
            "status_code": int(cached.get("status_code") or 200),
            "models": cached_models,
            "model_count": len(cached_models),
            "error": None,
            "generated_at": cached_ts,
        }
    if not cfg.api_key:
        return {
            "ok": False,
            "source": "unconfigured",
            "provider": "antigravity",
            "base_url": cfg.base_url,
            "endpoint": cfg.models_endpoint,
            "status_code": None,
            "models": cached_models,
            "model_count": len(cached_models),
            "error": "ANTIGRAVITY_API_KEY not set",
            "generated_at": cached_ts or None,
        }
    try:
        response = httpx.get(cfg.models_endpoint, headers=antigravity_request_headers(cfg.api_key), timeout=timeout_sec)
    except Exception as exc:
        fallback = cached_models
        return {
            "ok": bool(fallback),
            "source": "cache_fallback" if fallback else "network_error",
            "provider": "antigravity",
            "base_url": cfg.base_url,
            "endpoint": cfg.models_endpoint,
            "status_code": None,
            "models": fallback,
            "model_count": len(fallback),
            "error": str(exc),
            "generated_at": cached_ts or None,
        }
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    rows = payload.get("models") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = payload.get("data") if isinstance(payload, dict) else []
    live_models: list[str] = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or "").strip()
        if not name:
            continue
        normalized = _normalize_model_name(name)
        if _looks_like_model_name(normalized):
            live_models.append(normalized)
    live_models = _filter_models(live_models)
    if response.status_code >= 400:
        message = ""
        if isinstance(payload, dict):
            error_obj = payload.get("error") or {}
            if isinstance(error_obj, dict):
                message = str(error_obj.get("message") or "").strip()
        message = message or str(response.text or f"http_{response.status_code}").strip()
        fallback = cached_models
        return {
            "ok": bool(fallback),
            "source": "cache_fallback" if fallback else "live_http_error",
            "provider": "antigravity",
            "base_url": cfg.base_url,
            "endpoint": cfg.models_endpoint,
            "status_code": int(response.status_code),
            "models": fallback,
            "model_count": len(fallback),
            "error": message,
            "generated_at": cached_ts or None,
        }
    ts = int(time.time())
    _save_cache(live_models, status_code=int(response.status_code), endpoint=cfg.models_endpoint)
    return {
        "ok": True,
        "source": "live",
        "provider": "antigravity",
        "base_url": cfg.base_url,
        "endpoint": cfg.models_endpoint,
        "status_code": int(response.status_code),
        "models": live_models,
        "model_count": len(live_models),
        "error": None,
        "generated_at": ts,
    }


def extract_antigravity_response_text(payload: Any) -> str:
    return extract_provider_response_text(payload)


def invoke_antigravity_native(model_name: str, prompt: str, *, timeout_sec: float = 45.0, max_completion_tokens: int = 1200, temperature: float = 0.2) -> tuple[dict[str, Any] | None, str | None, int | None]:
    cfg = resolve_antigravity_provider_config()
    if not cfg.api_key:
        return None, "ANTIGRAVITY_API_KEY not set", None
    if not has_meaningful_request_payload(prompt):
        return None, EMPTY_PROVIDER_REQUEST_ERROR, None
    normalized_model = _normalize_model_name(model_name)
    try:
        response = httpx.post(
            cfg.chat_completions_endpoint,
            headers=antigravity_request_headers(cfg.api_key),
            json={
                "model": normalized_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_completion_tokens": max_completion_tokens,
                "temperature": temperature,
                "stream": False,
            },
            timeout=timeout_sec,
        )
    except Exception as exc:
        return None, str(exc), None
    try:
        payload = response.json()
    except Exception:
        payload = None
    if response.status_code >= 400:
        if isinstance(payload, dict):
            err = payload.get("error") or {}
            message = str(err.get("message") or response.text or f"http_{response.status_code}").strip()
            param = str(err.get("param") or "").strip()
            if param:
                message = f"{message}: {param}"
            return payload, message, response.status_code
        return None, (response.text or f"http_{response.status_code}").strip(), response.status_code
    if isinstance(payload, dict) and not provider_response_has_assistant_content_or_tool_calls(payload):
        return payload, EMPTY_ASSISTANT_RESPONSE_ERROR, response.status_code
    return payload if isinstance(payload, dict) else None, None, response.status_code
