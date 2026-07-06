from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .provider_credentials import sync_provider_env_aliases
from .openai_bazzite_endpoint import load_openai_endpoint_discovery


@dataclass(slots=True)
class OpenAIProviderConfig:
    api_key: str
    base_url: str
    models_endpoint: str
    chat_completions_endpoint: str
    responses_endpoint: str
    messages_endpoint: str
    messages_count_tokens_endpoint: str
    codex_endpoint: str
    default_model: str

    def endpoint_map(self) -> dict[str, str]:
        return {
            "models": self.models_endpoint,
            "chat_completions": self.chat_completions_endpoint,
            "responses": self.responses_endpoint,
            "messages": self.messages_endpoint,
            "messages_count_tokens": self.messages_count_tokens_endpoint,
            "codex": self.codex_endpoint,
        }


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


def _is_local_proxy_base_url(value: str) -> bool:
    parsed = urlparse(str(value or '').strip())
    host = (parsed.hostname or '').strip().lower()
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return host in {'127.0.0.1', 'localhost', 'host.containers.internal'} and port == 8012


def _default_direct_openai_base_url() -> str:
    explicit = _normalize_url(_first_env('OPENAI_DIRECT_BASE_URL', 'AI_BRIDGE_OPENAI_DIRECT_BASE_URL'))
    if explicit:
        return explicit
    return 'https://api.openai.com/v1'


def _endpoint_matches_base_url(endpoint: str, base_url: str) -> bool:
    normalized_endpoint = _normalize_url(endpoint)
    normalized_base = _normalize_url(base_url)
    return bool(normalized_endpoint and normalized_base and normalized_endpoint.startswith(f"{normalized_base}/"))


def _endpoint_from_base(explicit_endpoint: str, base_url: str, suffix: str) -> str:
    normalized = _normalize_url(explicit_endpoint)
    if _endpoint_matches_base_url(normalized, base_url):
        return normalized
    return _join_url(base_url, suffix)


def resolve_openai_provider_config() -> OpenAIProviderConfig:
    sync_provider_env_aliases(os.environ)

    discovery = load_openai_endpoint_discovery()
    api_key = _first_env("OPENAI_API_KEY", "CODEX_SALE_API_KEY")
    if not api_key:
        api_key = str(discovery.get("api_key") or "").strip()
    code_sale_base = _normalize_url(_first_env("CODEX_SALE_BASE_URL"))
    if not code_sale_base:
        code_sale_base = _normalize_url(str(discovery.get("codex_root_url") or ""))
    base_url = _normalize_url(_first_env("OPENAI_BASE_URL", "AI_BRIDGE_OPENAI_BASE_URL"))
    if not base_url:
        base_url = _normalize_url(str(discovery.get("base_url") or ""))
    if base_url and _is_local_proxy_base_url(base_url):
        allow_local_proxy = os.getenv("AI_BRIDGE_OPENAI_ALLOW_LOCAL_PROXY", "false").strip().lower() in {"1", "true", "yes", "on"}
        if not allow_local_proxy:
            base_url = _default_direct_openai_base_url()
    if not base_url and code_sale_base:
        base_url = _join_url(code_sale_base, "v1")
    if not base_url:
        base_url = _default_direct_openai_base_url()
    if not code_sale_base and base_url.endswith('/v1'):
        code_sale_base = base_url[:-3].rstrip('/')

    models_endpoint = _endpoint_from_base(_first_env("AI_BRIDGE_OPENAI_MODELS_ENDPOINT"), base_url, "models")
    chat_endpoint = _endpoint_from_base(_first_env("AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT"), base_url, "chat/completions")
    responses_endpoint = _endpoint_from_base(_first_env("AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT"), base_url, "responses")
    messages_endpoint = _endpoint_from_base(_first_env("AI_BRIDGE_OPENAI_MESSAGES_ENDPOINT"), base_url, "messages")
    count_tokens_endpoint = _endpoint_from_base(_first_env("AI_BRIDGE_OPENAI_MESSAGES_COUNT_TOKENS_ENDPOINT"), messages_endpoint, "count_tokens")
    codex_endpoint = _normalize_url(_first_env("AI_BRIDGE_OPENAI_CODEX_ENDPOINT", "CODEX_SALE_CODEX_ENDPOINT")) or _join_url(code_sale_base, "backend-api/codex")
    default_model = _first_env("CODEX_OPENAI_MODEL", "OPENAI_DEFAULT_MODEL") or str(discovery.get("default_model") or "") or "gpt-5.5"

    return OpenAIProviderConfig(
        api_key=api_key,
        base_url=base_url,
        models_endpoint=models_endpoint,
        chat_completions_endpoint=chat_endpoint,
        responses_endpoint=responses_endpoint,
        messages_endpoint=messages_endpoint,
        messages_count_tokens_endpoint=count_tokens_endpoint,
        codex_endpoint=codex_endpoint,
        default_model=default_model,
    )


def resolve_openai_provider_identity(config: OpenAIProviderConfig | None = None) -> dict[str, str]:
    cfg = config or resolve_openai_provider_config()
    provider_id = _first_env("AI_BRIDGE_OPENAI_PROVIDER_ID", "CODEX_PROVIDER", "AI_BRIDGE_CODEX_PROVIDER").strip().lower()
    discovery = load_openai_endpoint_discovery()
    discovery_source = str(discovery.get("source") or "").strip().lower()
    base_url = _normalize_url(getattr(cfg, "base_url", ""))
    codex_endpoint = _normalize_url(getattr(cfg, "codex_endpoint", ""))

    if provider_id in {"codex-sale", "codex_sale"}:
        provider_id = "codexsale"

    if not provider_id:
        if "codex.sale" in base_url or "codex.sale" in codex_endpoint or "codex-sale" in discovery_source:
            provider_id = "codexsale"
        else:
            provider_id = "openai"

    provider_name = {
        "codexsale": "Codex Sale",
        "openai": "OpenAI",
    }.get(provider_id, provider_id.replace('-', ' ').replace('_', ' ').title() or 'OpenAI')
    return {"provider_id": provider_id, "provider_name": provider_name}


def openai_endpoint_manifest(config: OpenAIProviderConfig | None = None) -> dict[str, object]:
    cfg = config or resolve_openai_provider_config()
    if hasattr(cfg, "endpoint_map"):
        endpoints = cfg.endpoint_map()
    else:
        endpoints = {
            "models": getattr(cfg, "models_endpoint", ""),
            "chat_completions": getattr(cfg, "chat_completions_endpoint", ""),
            "responses": getattr(cfg, "responses_endpoint", ""),
            "messages": getattr(cfg, "messages_endpoint", ""),
            "messages_count_tokens": getattr(cfg, "messages_count_tokens_endpoint", ""),
            "codex": getattr(cfg, "codex_endpoint", ""),
        }
    identity = resolve_openai_provider_identity(cfg)
    return {
        "provider": "openai",
        "provider_id": identity["provider_id"],
        "provider_name": identity["provider_name"],
        "base_url": getattr(cfg, "base_url", ""),
        "default_model": getattr(cfg, "default_model", ""),
        "endpoints": endpoints,
        "endpoint_roles": {
            "inventory": "models",
            "chat": "chat_completions",
            "responses": "responses",
            "claude_messages": "messages",
            "claude_count_tokens": "messages_count_tokens",
            "codex": "codex",
        },
    }


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
    target = config.base_url or config.models_endpoint or config.codex_endpoint
    if not target:
        return "api.openai.com:443"
    parsed = urlparse(target)
    host = (parsed.hostname or "").strip() or "api.openai.com"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"
