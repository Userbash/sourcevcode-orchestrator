from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .mimo_provider import configured_native_mimo_models, fetch_mimo_model_catalog, load_mimo_model_catalog, mimo_group_description, mimo_group_use_case, resolve_mimo_provider_config


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def mimo_enabled() -> bool:
    return _env_flag('AI_BRIDGE_MIMO_ENABLED', True)


def mimo_failure_threshold() -> int:
    return _env_int('AI_BRIDGE_MIMO_SUPPRESS_AFTER_FAILURES', 3, minimum=1)


def mimo_failure_window_sec() -> int:
    return _env_int('AI_BRIDGE_MIMO_FAILURE_WINDOW_SEC', 900, minimum=60)


def mimo_suppression_ttl_sec() -> int:
    return _env_int('AI_BRIDGE_MIMO_SUPPRESSION_TTL_SEC', 1800, minimum=60)


def default_report_dir() -> Path:
    workspace = Path('/workspace/reports/model_ping')
    if workspace.parent.exists():
        return workspace
    return Path.cwd() / 'reports' / 'model_ping'


def classify_mimo_error(error: str) -> str:
    text = str(error or '').strip().lower()
    if not text:
        return 'unknown'
    if 'personal access tokens are not supported' in text:
        return 'github_pat_not_supported'
    if 'google generative ai api key is missing' in text:
        return 'gemini_api_key_missing'
    if 'invalid api key' in text:
        return 'invalid_api_key'
    if 'illegal access' in text:
        return 'illegal_access'
    if 'no access to model' in text:
        return 'no_model_access'
    if 'invalid model' in text:
        return 'invalid_model'
    if 'token plan' in text:
        return 'token_plan_base_url_missing'
    if 'timeout' in text:
        return 'timeout'
    if 'no_text_events' in text:
        return 'no_text_events'
    return 'other'


def _provider_from_model_name(model_name: str) -> str:
    raw = str(model_name or '').strip()
    if '/' in raw:
        return raw.split('/', 1)[0].strip().lower() or 'unknown'
    return 'unknown'


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def load_mimo_ping_report(report_dir: str | Path | None = None) -> dict[str, Any]:
    base = Path(report_dir) if report_dir is not None else default_report_dir()
    path = base / 'mimo_model_ping_report.json'
    payload = _load_json(path) if path.is_file() else {}
    if payload:
        payload['_report_path'] = str(path)
        payload['_report_present'] = True
        payload['_report_updated_at'] = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    else:
        payload = {'_report_path': str(path), '_report_present': False, '_report_updated_at': None, 'models': [], 'ok': 0, 'failed': 0}
    return payload


def load_mimo_usable_report(report_dir: str | Path | None = None) -> dict[str, Any]:
    base = Path(report_dir) if report_dir is not None else default_report_dir()
    path = base / 'mimo_usable_models.json'
    payload = _load_json(path) if path.is_file() else {}
    if payload:
        payload['_usable_path'] = str(path)
        payload['_usable_present'] = True
        payload['_usable_updated_at'] = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    else:
        payload = {'_usable_path': str(path), '_usable_present': False, '_usable_updated_at': None, 'models': [], 'usable_count': 0, 'total': 0}
    return payload


def build_mimo_runtime_status(*, bridge: Any | None = None, report_dir: str | Path | None = None, status_source_configured: bool = False, failure_reason: str | None = None, recovery_attempts: int = 0, last_sync_at: str | None = None, profiles_loaded: int | None = None) -> dict[str, Any]:
    cfg = resolve_mimo_provider_config()
    if cfg.api_key and report_dir is None:
        try:
            from .provider_inventory_service import ProviderInventoryService
            ProviderInventoryService().refresh_mimo_usable_snapshot(force_refresh=True)
        except Exception:
            pass
    catalog = fetch_mimo_model_catalog(force_refresh=False) if (cfg.api_key and report_dir is None) else load_mimo_model_catalog()
    configured_models = [str(item).strip() for item in (catalog.get('models') or configured_native_mimo_models()) if str(item).strip()]
    if not mimo_enabled():
        return {
            'provider': 'mimo', 'status': 'disabled', 'ready': False, 'disabled_by_env': True, 'disable_env': 'AI_BRIDGE_MIMO_ENABLED', 'direct_api_configured': bool(cfg.api_key), 'base_url': cfg.base_url, 'status_source_configured': bool(status_source_configured), 'failure_reason': 'mimo_disabled_by_env', 'recovery_attempts': int(recovery_attempts or 0), 'last_sync_at': last_sync_at, 'profiles_loaded': int(profiles_loaded or 0), 'report_present': False, 'report_path': str((Path(report_dir) if report_dir is not None else default_report_dir()) / 'mimo_model_ping_report.json'), 'report_updated_at': None, 'usable_artifact_present': False, 'usable_artifact_path': str((Path(report_dir) if report_dir is not None else default_report_dir()) / 'mimo_usable_models.json'), 'usable_artifact_updated_at': None, 'inventory_count': 0, 'usable_count': 0, 'failed_count': 0, 'usable_models_sample': [], 'failed_models_sample': [], 'auth_categories': {}, 'provider_breakdown': {}, 'cached_models_count': 0, 'cached_models_sample': [], 'inventory_snapshot_present': False, 'inventory_snapshot_models_sample': [], 'live_inventory_count': 0, 'live_inventory_sample': [], 'live_inventory_error': 'mimo_disabled_by_env', 'suppression_policy': {'failure_threshold': mimo_failure_threshold(), 'failure_window_sec': mimo_failure_window_sec(), 'suppression_ttl_sec': mimo_suppression_ttl_sec()}}
    report = load_mimo_ping_report(report_dir)
    usable_artifact = load_mimo_usable_report(report_dir)
    rows = list(report.get('models') or [])
    if not rows:
        usable_rows = []
        for row in list(usable_artifact.get('models') or []):
            if isinstance(row, dict) and str(row.get('model') or '').strip():
                usable_rows.append({'model': str(row.get('model')).strip(), 'ok': True, 'response_sample': row.get('response_sample'), 'status_code': row.get('status_code')})
        if usable_rows:
            rows = usable_rows
            report = {**report, 'models': rows, 'ok': len(rows), 'failed': 0}
    usable_rows = [row for row in rows if isinstance(row, dict) and row.get('ok')]
    failed_rows = [row for row in rows if isinstance(row, dict) and not row.get('ok')]
    group_rows = list(report.get('groups') or []) if isinstance(report, dict) else []
    group_overview = [
        {
            'group': str(row.get('group') or '').strip(),
            'description': str(row.get('description') or mimo_group_description(str(row.get('group') or ''))),
            'use_case': str(row.get('use_case') or mimo_group_use_case(str(row.get('group') or ''))),
            'probe_mode': str(row.get('probe_mode') or ''),
            'inventory_count': int(row.get('inventory_count') or 0),
            'probed_count': int(row.get('probed_count') or 0),
            'ready_count': int(row.get('ready_count') or 0),
            'failed_count': int(row.get('failed_count') or 0),
            'models': [str(item).strip() for item in (row.get('models') or []) if str(item).strip()],
            'ready_models': [str(item).strip() for item in (row.get('ready_models') or []) if str(item).strip()],
            'failed_models': row.get('failed_models') or [],
        }
        for row in group_rows if isinstance(row, dict)
    ]
    auth_categories: dict[str, int] = {}
    provider_breakdown: dict[str, dict[str, int]] = {}
    for row in rows:
        provider = _provider_from_model_name(str((row or {}).get('model') or ''))
        bucket = provider_breakdown.setdefault(provider, {'total': 0, 'ok': 0, 'failed': 0})
        bucket['total'] += 1
        if row.get('ok'):
            bucket['ok'] += 1
        else:
            bucket['failed'] += 1
            category = classify_mimo_error(str(row.get('error') or ''))
            auth_categories[category] = auth_categories.get(category, 0) + 1
    from .provider_inventory_service import ProviderInventoryService
    inventory_snapshot = ProviderInventoryService().provider_snapshot('mimo')
    snapshot_models = [str(item).strip() for item in (inventory_snapshot.get('models', []) if isinstance(inventory_snapshot, dict) else []) if str(item).strip()]
    usable_count = len(usable_rows)
    failed_count = len(failed_rows)
    text_ready_count = int(report.get('text_ready_count') or 0)
    specialized_ready_count = int(report.get('specialized_ready_count') or 0)
    if not text_ready_count and group_overview:
        text_ready_count = sum(int(row.get('ready_count') or 0) for row in group_overview if str(row.get('group') or '') in {'text', 'multimodal'})
    if not specialized_ready_count and group_overview:
        specialized_ready_count = sum(int(row.get('ready_count') or 0) for row in group_overview if str(row.get('group') or '') in {'asr', 'tts'})
    inventory_count = len(rows) or len(snapshot_models) or len(configured_models)
    auth_only_failure = failed_count > 0 and usable_count == 0 and bool(auth_categories) and sum(auth_categories.values()) >= failed_count
    if usable_count > 0:
        status = 'degraded' if failed_count > 0 else 'ready'
        ready = True
    elif auth_only_failure:
        status = 'failed'
        ready = False
    elif bool(cfg.api_key) and bool(configured_models):
        status = 'degraded' if failed_count > 0 else 'ready'
        ready = True
    elif configured_models:
        status = 'inventory_unknown'
        ready = False
    else:
        status = 'offline'
        ready = False
    live_inventory_error = str(catalog.get('error') or '') or (None if configured_models else 'native_model_catalog_empty')
    return {
        'provider': 'mimo', 'status': status, 'ready': ready, 'disabled_by_env': False, 'disable_env': 'AI_BRIDGE_MIMO_ENABLED', 'direct_api_configured': bool(cfg.api_key), 'base_url': cfg.base_url, 'base_url_explicit': cfg.base_url_explicit, 'key_kind': cfg.key_kind, 'status_source_configured': bool(status_source_configured), 'failure_reason': failure_reason, 'recovery_attempts': int(recovery_attempts or 0), 'last_sync_at': last_sync_at, 'profiles_loaded': int(profiles_loaded or 0), 'report_present': bool(report.get('_report_present')), 'report_path': str(report.get('_report_path') or ''), 'report_updated_at': report.get('_report_updated_at'), 'usable_artifact_present': bool(usable_artifact.get('_usable_present')), 'usable_artifact_path': str(usable_artifact.get('_usable_path') or ''), 'usable_artifact_updated_at': usable_artifact.get('_usable_updated_at'), 'inventory_count': inventory_count, 'usable_count': usable_count, 'failed_count': failed_count, 'usable_models_sample': [str(row.get('model') or '') for row in usable_rows[:12]], 'text_ready_count': text_ready_count, 'specialized_ready_count': specialized_ready_count, 'failed_models_sample': [{'model': str(row.get('model') or ''), 'error': str(row.get('error') or ''), 'status_code': row.get('status_code')} for row in failed_rows[:12]], 'auth_categories': auth_categories, 'provider_breakdown': provider_breakdown, 'cached_models_count': 0, 'cached_models_sample': [], 'group_overview': group_overview, 'group_ready_counts': {str(row.get('group') or '').strip(): int(row.get('ready_count') or 0) for row in group_overview if str(row.get('group') or '').strip()}, 'group_failed_counts': {str(row.get('group') or '').strip(): int(row.get('failed_count') or 0) for row in group_overview if str(row.get('group') or '').strip()}, 'inventory_snapshot_present': bool(snapshot_models), 'inventory_snapshot_models_sample': snapshot_models[:12], 'live_inventory_count': len(configured_models), 'live_inventory_sample': configured_models[:12], 'live_inventory_source': str(catalog.get('source') or 'cache'), 'live_inventory_error': live_inventory_error, 'suppression_policy': {'failure_threshold': mimo_failure_threshold(), 'failure_window_sec': mimo_failure_window_sec(), 'suppression_ttl_sec': mimo_suppression_ttl_sec()}}
