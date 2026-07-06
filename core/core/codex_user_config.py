from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


@dataclass(slots=True)
class CodexUserSettings:
    resolved_dir: Path | None
    config_path: Path | None
    auth_path: Path | None
    env_path: Path | None


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        out.append(path.expanduser())
    return out


def candidate_codex_dirs() -> list[Path]:
    explicit = [
        Path(value).expanduser()
        for value in (
            os.getenv('AI_BRIDGE_CODEX_CONFIG_DIR', '').strip(),
            os.getenv('CODEX_HOME', '').strip(),
        )
        if value.strip()
    ]
    home = Path.home()
    defaults = [
        home / '.var' / 'app' / 'com.visualstudio.code' / 'config' / '.codex',
        home / '.codex',
    ]
    return _dedupe_paths([*explicit, *defaults])


def resolve_codex_settings() -> CodexUserSettings:
    for directory in candidate_codex_dirs():
        config_path = directory / 'config.toml'
        auth_path = directory / 'auth.json'
        env_path = directory / 'codex-sale.env'
        if any(path.exists() for path in (config_path, auth_path, env_path)):
            return CodexUserSettings(
                resolved_dir=directory,
                config_path=config_path if config_path.exists() else None,
                auth_path=auth_path if auth_path.exists() else None,
                env_path=env_path if env_path.exists() else None,
            )
    return CodexUserSettings(resolved_dir=None, config_path=None, auth_path=None, env_path=None)


def _set_if_missing(target: MutableMapping[str, str], key: str, value: str, *, override: bool) -> None:
    if not key or not str(value).strip():
        return
    if override or not str(target.get(key, '')).strip():
        target[key] = str(value).strip()


def _parse_env_exports(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _parse_auth_json(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    values: dict[str, str] = {}
    for key in ('OPENAI_API_KEY', 'CODEX_LB_API_KEY', 'CODEX_SALE_API_KEY'):
        raw = str(payload.get(key) or '').strip()
        if raw:
            values[key] = raw
    return values


def _parse_config_toml(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists() or tomllib is None:
        return {}
    try:
        payload = tomllib.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_url(value: str) -> str:
    return str(value or '').strip().rstrip('/')


def _join_url(base: str, suffix: str) -> str:
    base_clean = _normalize_url(base)
    if not base_clean:
        return ''
    return f"{base_clean}/{suffix.lstrip('/')}"


def _root_from_provider_url(value: str) -> str:
    clean = _normalize_url(value)
    if not clean:
        return ''
    for suffix in ('/backend-api/codex', '/backend-api/openai', '/backend-api/responses'):
        if clean.endswith(suffix):
            return clean[: -len(suffix)]
    marker = '/backend-api/'
    if marker in clean:
        return clean.split(marker, 1)[0]
    if clean.endswith('/v1'):
        return clean[:-3].rstrip('/')
    return clean


def _derive_endpoint_values(config_payload: dict[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    model_provider = str(config_payload.get('model_provider') or '').strip()
    model = str(config_payload.get('model') or '').strip()
    reasoning_effort = str(config_payload.get('model_reasoning_effort') or '').strip()
    providers = config_payload.get('model_providers') if isinstance(config_payload.get('model_providers'), dict) else {}
    provider_cfg = providers.get(model_provider) if isinstance(providers, dict) else {}
    provider_cfg = provider_cfg if isinstance(provider_cfg, dict) else {}
    orchestrator_routing = config_payload.get('orchestrator_routing') if isinstance(config_payload.get('orchestrator_routing'), dict) else {}
    orchestrator_routing = orchestrator_routing if isinstance(orchestrator_routing, dict) else {}

    raw_base_url = _normalize_url(str(provider_cfg.get('base_url') or ''))
    root_url = _root_from_provider_url(raw_base_url)
    openai_base_url = _join_url(root_url, 'v1') if root_url else ''

    explicit_models = _normalize_url(str(orchestrator_routing.get('models_endpoint') or ''))
    explicit_chat = _normalize_url(str(orchestrator_routing.get('chat_completions_endpoint') or ''))
    explicit_responses = _normalize_url(str(orchestrator_routing.get('responses_endpoint') or ''))

    if openai_base_url:
        values['CODEX_SALE_BASE_URL'] = root_url
        values['CODEX_ROOT_URL'] = root_url
        values['OPENAI_BASE_URL'] = openai_base_url
        values['AI_BRIDGE_OPENAI_BASE_URL'] = openai_base_url
        values['AI_BRIDGE_OPENAI_MODELS_ENDPOINT'] = explicit_models or _join_url(openai_base_url, 'models')
        values['AI_BRIDGE_OPENAI_CHAT_COMPLETIONS_ENDPOINT'] = explicit_chat or _join_url(openai_base_url, 'chat/completions')
        values['AI_BRIDGE_OPENAI_RESPONSES_ENDPOINT'] = explicit_responses or _join_url(openai_base_url, 'responses')

    env_key = str(provider_cfg.get('env_key') or '').strip()
    if env_key:
        values['CODEX_LB_ENV_KEY'] = env_key
    wire_api = str(provider_cfg.get('wire_api') or '').strip()
    if wire_api:
        values['AI_BRIDGE_OPENAI_WIRE_API'] = wire_api
    if model:
        values['CODEX_OPENAI_MODEL'] = model
    if reasoning_effort:
        values['MODEL_REASONING_EFFORT'] = reasoning_effort
    if model_provider:
        values['AI_BRIDGE_OPENAI_PROVIDER_ID'] = model_provider
    return values


def sync_codex_user_env(target: MutableMapping[str, str] | None = None, *, override: bool = False) -> MutableMapping[str, str]:
    env = target if target is not None else os.environ
    settings = resolve_codex_settings()
    if settings.resolved_dir is None:
        return env

    _set_if_missing(env, 'AI_BRIDGE_CODEX_CONFIG_DIR_RESOLVED', str(settings.resolved_dir), override=override)

    env_values = _parse_env_exports(settings.env_path)
    auth_values = _parse_auth_json(settings.auth_path)
    config_values = _derive_endpoint_values(_parse_config_toml(settings.config_path))

    discovered_key = (
        env_values.get('CODEX_LB_API_KEY')
        or auth_values.get('CODEX_LB_API_KEY')
        or env_values.get('OPENAI_API_KEY')
        or auth_values.get('OPENAI_API_KEY')
        or auth_values.get('CODEX_SALE_API_KEY')
    )
    env_key_name = config_values.get('CODEX_LB_ENV_KEY') or 'CODEX_LB_API_KEY'
    if discovered_key:
        env_values.setdefault(str(env_key_name), discovered_key)
        env_values.setdefault('CODEX_LB_API_KEY', discovered_key)
        env_values.setdefault('OPENAI_API_KEY', discovered_key)
        env_values.setdefault('CODEX_SALE_API_KEY', discovered_key)

    merged = {**config_values, **env_values}
    for key, value in merged.items():
        _set_if_missing(env, key, str(value), override=override)
    for key, value in auth_values.items():
        _set_if_missing(env, key, value, override=override)

    mirrored_key = str(env.get('CODEX_LB_API_KEY', '')).strip() or str(env.get('OPENAI_API_KEY', '')).strip()
    if mirrored_key:
        for alias in ('CODEX_LB_API_KEY', 'OPENAI_API_KEY', 'CODEX_SALE_API_KEY'):
            _set_if_missing(env, alias, mirrored_key, override=override)

    return env
