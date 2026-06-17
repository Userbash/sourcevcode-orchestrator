from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIMO_CLI_CANDIDATES = (
    '/var/home/sanya/.npm-packages/bin/mimo',
    '/root/.npm-packages/bin/mimo',
    '/usr/local/bin/mimo',
)


def resolve_mimo_cli() -> str | None:
    found = shutil.which('mimo')
    if found:
        return found
    for candidate in MIMO_CLI_CANDIDATES:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    var_home = Path('/var/home')
    if var_home.is_dir():
        for home in var_home.iterdir():
            candidate = home / '.npm-packages' / 'bin' / 'mimo'
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


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
    if 'no access to model' in text:
        return 'no_model_access'
    if 'invalid model' in text:
        return 'invalid_model'
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
        payload = {
            '_report_path': str(path),
            '_report_present': False,
            '_report_updated_at': None,
            'models': [],
            'ok': 0,
            'failed': 0,
        }
    return payload


def build_mimo_runtime_status(
    *,
    bridge: Any | None = None,
    report_dir: str | Path | None = None,
    status_source_configured: bool = False,
    failure_reason: str | None = None,
    recovery_attempts: int = 0,
    last_sync_at: str | None = None,
    profiles_loaded: int | None = None,
) -> dict[str, Any]:
    cli_path = resolve_mimo_cli()
    report = load_mimo_ping_report(report_dir)
    rows = list(report.get('models') or [])
    usable_rows = [row for row in rows if isinstance(row, dict) and row.get('ok')]
    failed_rows = [row for row in rows if isinstance(row, dict) and not row.get('ok')]
    auth_categories: dict[str, int] = {}
    provider_breakdown: dict[str, dict[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = _provider_from_model_name(str(row.get('model') or ''))
        bucket = provider_breakdown.setdefault(provider, {'total': 0, 'ok': 0, 'failed': 0})
        bucket['total'] += 1
        if row.get('ok'):
            bucket['ok'] += 1
        else:
            bucket['failed'] += 1
            category = classify_mimo_error(str(row.get('error') or ''))
            auth_categories[category] = auth_categories.get(category, 0) + 1

    cached_models = list(bridge.get_cached_models()) if bridge is not None and hasattr(bridge, 'get_cached_models') else []
    cached_names = []
    for item in cached_models:
        model_name = str(getattr(item, 'full_id', '') or getattr(item, 'id', '')).strip()
        if model_name:
            cached_names.append(model_name)

    cli_alive = bool(getattr(bridge, 'is_cli_alive', False)) if bridge is not None else bool(cli_path)
    usable_count = len(usable_rows)
    failed_count = len(failed_rows)
    inventory_count = len(rows) or len(cached_names)

    if usable_count > 0:
        status = 'degraded' if failed_count > 0 else 'ready'
        ready = True
    elif inventory_count > 0 or report.get('_report_present'):
        status = 'degraded'
        ready = False
    elif cli_path:
        status = 'inventory_unknown'
        ready = False
    else:
        status = 'offline'
        ready = False

    return {
        'provider': 'mimo',
        'status': status,
        'ready': ready,
        'cli_available': bool(cli_path),
        'cli_path': cli_path,
        'bridge_cli_alive': cli_alive,
        'status_source_configured': bool(status_source_configured),
        'failure_reason': failure_reason,
        'recovery_attempts': int(recovery_attempts or 0),
        'last_sync_at': last_sync_at,
        'profiles_loaded': int(profiles_loaded or 0),
        'report_present': bool(report.get('_report_present')),
        'report_path': str(report.get('_report_path') or ''),
        'report_updated_at': report.get('_report_updated_at'),
        'inventory_count': inventory_count,
        'usable_count': usable_count,
        'failed_count': failed_count,
        'usable_models_sample': [str(row.get('model') or '') for row in usable_rows[:12]],
        'failed_models_sample': [
            {'model': str(row.get('model') or ''), 'error': str(row.get('error') or ''), 'exit_code': row.get('exit_code')}
            for row in failed_rows[:12]
        ],
        'auth_categories': auth_categories,
        'provider_breakdown': provider_breakdown,
        'cached_models_count': len(cached_names),
        'cached_models_sample': cached_names[:12],
    }
