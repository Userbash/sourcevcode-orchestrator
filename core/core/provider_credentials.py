from __future__ import annotations

import os
from typing import Iterable, MutableMapping

from .openai_bazzite_endpoint import load_openai_endpoint_discovery
from .codex_user_config import sync_codex_user_env
from urllib.parse import urlparse


_ANTIGRAVITY_KEY_ENV_NAMES = (
    "ANTIGRAVITY_API_KEY",
    "ANTIGRAVITY_API_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)

_GITHUB_TOKEN_ENV_NAMES = (
    "GITHUB_API",
    "GITHUB_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HOST_BRIDGE_GH_TOKEN",
)

_OPENAI_KEY_ENV_NAMES = (
    "OPENAI_API_KEY",
    "CODEX_SALE_API_KEY",
)

_OPENAI_BASE_URL_ENV_NAMES = (
    "OPENAI_BASE_URL",
    "AI_BRIDGE_OPENAI_BASE_URL",
)

_GENERIC_PLACEHOLDER_MARKERS = (
    "your_",
    "replace_",
    "changeme",
    "example",
    "placeholder",
    "<api",
    "<token",
    "<key",
)


def _first_value(target: MutableMapping[str, str], env_names: Iterable[str]) -> str:
    for env_name in env_names:
        value = str(target.get(env_name, "")).strip()
        if value:
            return value
    return ""


def _normalize_base_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _join_url(base: str, suffix: str) -> str:
    base_clean = _normalize_base_url(base)
    if not base_clean:
        return ""
    return f"{base_clean}/{suffix.lstrip('/')}"


def _derive_openai_base_url(target: MutableMapping[str, str]) -> str:
    explicit = _first_value(target, _OPENAI_BASE_URL_ENV_NAMES)
    if explicit:
        return _normalize_base_url(explicit)

    code_sale_base = _normalize_base_url(str(target.get("CODEX_SALE_BASE_URL", "")))
    if code_sale_base:
        return _join_url(code_sale_base, "v1")
    return ""


def _derive_tcp_probe_hosts(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").strip()
    if not host:
        return "api.openai.com:443"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"


def sync_provider_env_aliases(
    env: MutableMapping[str, str] | None = None,
    *,
    override: bool = False,
) -> MutableMapping[str, str]:
    target = env if env is not None else os.environ
    sync_codex_user_env(target, override=override)

    antigravity_key = _first_value(target, _ANTIGRAVITY_KEY_ENV_NAMES)
    if antigravity_key:
        for env_name in _ANTIGRAVITY_KEY_ENV_NAMES:
            if override or not str(target.get(env_name, "")).strip():
                target[env_name] = antigravity_key

    github_token = _first_value(target, _GITHUB_TOKEN_ENV_NAMES)
    if github_token:
        for env_name in _GITHUB_TOKEN_ENV_NAMES:
            if override or not str(target.get(env_name, "")).strip():
                target[env_name] = github_token

    openai_key = _first_value(target, _OPENAI_KEY_ENV_NAMES)
    if openai_key:
        for env_name in _OPENAI_KEY_ENV_NAMES:
            if override or not str(target.get(env_name, "")).strip():
                target[env_name] = openai_key

    openai_base_url = _derive_openai_base_url(target)
    if openai_base_url:
        for env_name in _OPENAI_BASE_URL_ENV_NAMES:
            if override or not str(target.get(env_name, "")).strip():
                target[env_name] = openai_base_url

        derived_endpoints = {
            "AI_BRIDGE_OPENAI_MODELS_ENDPOINT": _join_url(openai_base_url, "models"),
            "AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT": _join_url(openai_base_url, "chat/completions"),
            "AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT": _join_url(openai_base_url, "responses"),
        }
        for env_name, value in derived_endpoints.items():
            if value and (override or not str(target.get(env_name, "")).strip()):
                target[env_name] = value

        if override or not str(target.get("OPENAI_TCP_PROBE_HOSTS", "")).strip():
            target["OPENAI_TCP_PROBE_HOSTS"] = _derive_tcp_probe_hosts(openai_base_url)

    return target


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _discovered_openai_snapshot() -> dict[str, object]:
    payload = load_openai_endpoint_discovery()
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        return {}
    placeholder = _is_placeholder(api_key)
    return {
        "env_var": "OPENAI_ENDPOINT_DISCOVERY_PATH",
        "configured": True,
        "usable": bool(api_key and not placeholder),
        "placeholder": placeholder,
        "preview": _mask_secret(api_key),
        "source": str(payload.get("source") or "discovery"),
    }


def _is_placeholder(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in {"test", "demo", "dummy", "null", "none", "sk-test", "local"}:
        return True
    return any(marker in lowered for marker in _GENERIC_PLACEHOLDER_MARKERS)


def credential_snapshot(env_names: Iterable[str]) -> dict[str, object]:
    sync_provider_env_aliases(os.environ)
    for env_name in env_names:
        value = (os.getenv(env_name) or "").strip()
        if not value:
            continue
        placeholder = _is_placeholder(value)
        return {
            "env_var": env_name,
            "configured": True,
            "usable": not placeholder,
            "placeholder": placeholder,
            "preview": _mask_secret(value),
        }
    if any(name in _OPENAI_KEY_ENV_NAMES for name in env_names):
        discovered = _discovered_openai_snapshot()
        if discovered:
            return discovered
    return {
        "env_var": None,
        "configured": False,
        "usable": False,
        "placeholder": False,
        "preview": None,
    }


def has_usable_credential(*env_names: str) -> bool:
    return bool(credential_snapshot(env_names).get("usable"))
