from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .provider_credentials import sync_provider_env_aliases


@dataclass(slots=True)
class OpenAIProviderConfig:
    api_key: str
    base_url: str
    models_endpoint: str
    chat_completions_endpoint: str
    responses_endpoint: str
    default_model: str


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
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


def resolve_openai_provider_config() -> OpenAIProviderConfig:
    sync_provider_env_aliases(os.environ)

    api_key = _first_env("OPENAI_API_KEY", "CODEX_SALE_API_KEY")
    code_sale_base = _normalize_url(_first_env("CODEX_SALE_BASE_URL"))
    base_url = _normalize_url(_first_env("OPENAI_BASE_URL", "AI_BRIDGE_OPENAI_BASE_URL"))
    if not base_url and code_sale_base:
        base_url = _join_url(code_sale_base, "v1")

    models_endpoint = _normalize_url(_first_env("AI_BRIDGE_OPENAI_MODELS_ENDPOINT")) or _join_url(base_url, "models")
    chat_endpoint = _normalize_url(_first_env("AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT")) or _join_url(base_url, "chat/completions")
    responses_endpoint = _normalize_url(_first_env("AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT")) or _join_url(base_url, "responses")
    default_model = _first_env("CODEX_OPENAI_MODEL", "OPENAI_DEFAULT_MODEL") or "gpt-5-mini"

    return OpenAIProviderConfig(
        api_key=api_key,
        base_url=base_url,
        models_endpoint=models_endpoint,
        chat_completions_endpoint=chat_endpoint,
        responses_endpoint=responses_endpoint,
        default_model=default_model,
    )


def build_openai_client_kwargs(*, max_retries: int = 1) -> dict[str, object]:
    config = resolve_openai_provider_config()
    kwargs: dict[str, object] = {
        "api_key": config.api_key,
        "max_retries": max_retries,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return kwargs


def default_openai_tcp_probe_hosts() -> str:
    config = resolve_openai_provider_config()
    target = config.base_url or config.models_endpoint
    if not target:
        return "api.openai.com:443"
    parsed = urlparse(target)
    host = (parsed.hostname or "").strip() or "api.openai.com"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"
