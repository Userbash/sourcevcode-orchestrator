from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .codex_user_config import candidate_codex_dirs

_PLACEHOLDER_MARKERS = {"test", "demo", "dummy", "null", "none", "sk-test", "local"}
_GENERIC_PLACEHOLDER_MARKERS = ("your_", "replace_", "changeme", "example", "placeholder", "<api", "<token", "<key")
_OPENAI_KEY_NAMES = ("OPENAI_API_KEY", "CODEX_SALE_API_KEY", "CODEX_LB_API_KEY")
_OPENAI_BASE_NAMES = ("OPENAI_BASE_URL", "AI_BRIDGE_OPENAI_BASE_URL", "CODEX_SALE_BASE_URL")
_DEFAULT_MODEL_NAMES = ("CODEX_OPENAI_MODEL", "OPENAI_DEFAULT_MODEL")
_REASONING_NAMES = ("MODEL_REASONING_EFFORT",)
_CODEX_ROOT_NAMES = ("CODEX_ROOT_URL",)


@dataclass(slots=True)
class OpenAIEndpointDiscovery:
    api_key: str = ""
    base_url: str = ""
    default_model: str = "gpt-5.5"
    model_reasoning_effort: str = ""
    codex_root_url: str = ""
    usable: bool = False
    source: str = "unconfigured"
    sources_checked: list[str] | None = None
    endpoint_manifest: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "model_reasoning_effort": self.model_reasoning_effort,
            "codex_root_url": self.codex_root_url,
            "usable": self.usable,
            "source": self.source,
            "sources_checked": list(self.sources_checked or []),
            "endpoint_manifest": dict(self.endpoint_manifest or {}),
        }


def _normalize_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _join_url(base: str, suffix: str) -> str:
    base_clean = _normalize_url(base)
    if not base_clean:
        return ""
    return f"{base_clean}/{suffix.lstrip('/')}"


def _is_placeholder(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in _PLACEHOLDER_MARKERS:
        return True
    return any(marker in lowered for marker in _GENERIC_PLACEHOLDER_MARKERS)


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _read_env_file(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return payload
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    return payload


def _read_server_env(path: Path) -> dict[str, str]:
    payload = _read_env_file(path)
    if payload:
        return payload
    if not path.exists() or not path.is_file():
        return {}
    discovered: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        discovered[key.strip()] = value.strip().strip('"').strip("'")
    return discovered


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _first_value(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(mapping.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _flatten_toml(payload: dict[str, Any]) -> dict[str, str]:
    flat: dict[str, str] = {}
    env_aliases = {
        ("openai", "api_key"): "OPENAI_API_KEY",
        ("openai", "base_url"): "OPENAI_BASE_URL",
        ("openai", "default_model"): "OPENAI_DEFAULT_MODEL",
        ("openai", "codex_root_url"): "CODEX_ROOT_URL",
    }
    for top_key, top_value in payload.items():
        if isinstance(top_value, dict):
            for inner_key, inner_value in top_value.items():
                value = str(inner_value)
                flat[f"{top_key}.{inner_key}"] = value
                flat[inner_key] = flat.get(inner_key, value)
                alias = env_aliases.get((str(top_key).lower(), str(inner_key).lower()))
                if alias and value:
                    flat[alias] = value
        else:
            flat[top_key] = str(top_value)
    return flat


def _auth_json_to_env(payload: dict[str, Any]) -> dict[str, str]:
    flat: dict[str, str] = {}
    for key in _OPENAI_KEY_NAMES + _OPENAI_BASE_NAMES + _DEFAULT_MODEL_NAMES + _REASONING_NAMES + _CODEX_ROOT_NAMES:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            flat[key] = value.strip()
    openai = payload.get("openai") if isinstance(payload.get("openai"), dict) else {}
    for key, value in openai.items():
        if isinstance(value, str) and value.strip():
            normalized = key.upper()
            if normalized in {"API_KEY", "BASE_URL", "DEFAULT_MODEL"}:
                mapped = {
                    "API_KEY": "OPENAI_API_KEY",
                    "BASE_URL": "OPENAI_BASE_URL",
                    "DEFAULT_MODEL": "OPENAI_DEFAULT_MODEL",
                }[normalized]
                flat[mapped] = value.strip()
    return flat


def _default_discovery_path() -> Path:
    raw = os.getenv("OPENAI_ENDPOINT_DISCOVERY_PATH", "reports/openai_endpoint_discovery.json")
    return Path(raw)


def discover_openai_endpoint(*, home: Path | None = None, runtime_env: Mapping[str, str] | None = None) -> OpenAIEndpointDiscovery:
    base_home = home or Path.home()
    env = dict(os.environ if runtime_env is None else runtime_env)
    sources_checked: list[str] = []
    candidates: list[tuple[str, dict[str, str]]] = []

    env_candidate = {key: str(env.get(key, "") or "").strip() for key in (_OPENAI_KEY_NAMES + _OPENAI_BASE_NAMES + _DEFAULT_MODEL_NAMES + _REASONING_NAMES + _CODEX_ROOT_NAMES)}
    sources_checked.append("runtime_env")
    candidates.append(("runtime_env", env_candidate))

    codex_dirs = candidate_codex_dirs()
    if not codex_dirs:
        codex_dirs = [base_home / ".codex"]

    for codex_dir in codex_dirs:
        codex_env_path = codex_dir / "codex-sale.env"
        codex_env = _read_env_file(codex_env_path)
        sources_checked.append(str(codex_env_path))
        candidates.append((f"codex-sale.env:{codex_dir}", codex_env))

        config_path = codex_dir / "config.toml"
        config_toml = _flatten_toml(_read_toml(config_path))
        sources_checked.append(str(config_path))
        candidates.append((f"config.toml:{codex_dir}", config_toml))

        auth_path = codex_dir / "auth.json"
        auth_json = _auth_json_to_env(_read_json(auth_path))
        sources_checked.append(str(auth_path))
        candidates.append((f"auth.json:{codex_dir}", auth_json))

    server_env = _read_server_env(base_home / ".vscode-server" / "server-env-setup")
    sources_checked.append(str(base_home / ".vscode-server" / "server-env-setup"))
    candidates.append(("vscode-server-env", server_env))

    merged: dict[str, str] = {}
    source_name = "unconfigured"
    for name, payload in candidates:
        if not payload:
            continue
        for key, value in payload.items():
            if value and not merged.get(key):
                merged[key] = value
        if source_name == "unconfigured" and (_first_value(payload, _OPENAI_KEY_NAMES) or _first_value(payload, _OPENAI_BASE_NAMES)):
            source_name = name

    api_key = _first_value(merged, _OPENAI_KEY_NAMES)
    base_url = _normalize_url(_first_value(merged, ("OPENAI_BASE_URL", "AI_BRIDGE_OPENAI_BASE_URL")))
    code_sale_base = _normalize_url(_first_value(merged, ("CODEX_SALE_BASE_URL",)))
    if not base_url and code_sale_base:
        base_url = _join_url(code_sale_base, "v1")
    if not base_url:
        base_url = "https://api.openai.com/v1"
    default_model = _first_value(merged, _DEFAULT_MODEL_NAMES) or "gpt-5.5"
    codex_root = _normalize_url(_first_value(merged, _CODEX_ROOT_NAMES)) or code_sale_base
    reasoning = _first_value(merged, _REASONING_NAMES)
    usable = bool(api_key and not _is_placeholder(api_key))
    endpoint_manifest = {
        "models": _join_url(base_url, "models"),
        "chat_completions": _join_url(base_url, "chat/completions"),
        "responses": _join_url(base_url, "responses"),
        "messages": _join_url(base_url, "messages"),
        "messages_count_tokens": _join_url(base_url, "messages/count_tokens"),
        "codex": _join_url(codex_root, "backend-api/codex") if codex_root else "",
    }
    return OpenAIEndpointDiscovery(
        api_key=api_key,
        base_url=base_url,
        default_model=default_model,
        model_reasoning_effort=reasoning,
        codex_root_url=codex_root,
        usable=usable,
        source=source_name,
        sources_checked=sources_checked,
        endpoint_manifest=endpoint_manifest,
    )


def write_openai_endpoint_discovery(*, output_path: Path | None = None, home: Path | None = None, runtime_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    discovery = discover_openai_endpoint(home=home, runtime_env=runtime_env)
    payload = discovery.as_dict()
    payload["api_key_preview"] = _mask_secret(discovery.api_key) if discovery.api_key else None
    payload["api_key_present"] = bool(discovery.api_key)
    path = output_path or _default_discovery_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return payload


def load_openai_endpoint_discovery(path: Path | None = None) -> dict[str, Any]:
    target = path or _default_discovery_path()
    if not target.exists() or not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
