from __future__ import annotations

import base64
import io
import json
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR, extract_provider_response_text, has_meaningful_request_payload, provider_response_has_assistant_content_or_tool_calls


@dataclass(slots=True)
class MimoProviderConfig:
    api_key: str
    base_url: str
    models_endpoint: str
    chat_completions_endpoint: str
    default_model: str
    key_kind: str
    base_url_explicit: bool


def _first_env(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name, '') or '').strip()
        if value:
            return value
    return ''


def _normalize_url(value: str) -> str:
    return str(value or '').strip().rstrip('/')


def _join_url(base: str, suffix: str) -> str:
    base_clean = _normalize_url(base)
    if not base_clean:
        return ''
    return f"{base_clean}/{suffix.lstrip('/')}"


def _mimo_generated_profile_root() -> Path:
    return Path(os.getenv('MIMO_GENERATED_PROFILE_DIR', 'core/mimo/profiles/generated/mimo_native'))


def _mimo_templates_path(root: Path | None = None) -> Path:
    base = root or _mimo_generated_profile_root()
    return Path(os.getenv('MIMO_ORCHESTRATOR_TEMPLATES_PATH', str(base / 'orchestrator_templates.json')))


def _mimo_cache_path() -> Path:
    return Path(os.getenv('MIMO_MODELS_CACHE_PATH', 'core/.cache/mimo_models.json'))


def _mimo_full_cache_path() -> Path:
    return Path(os.getenv('MIMO_MODELS_FULL_CACHE_PATH', 'core/.cache/mimo_models_full.json'))


def _mimo_cache_ttl_sec() -> int:
    raw = str(os.getenv('MIMO_MODELS_CACHE_TTL_SEC', '900') or '900').strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 900


def mimo_key_kind(api_key: str) -> str:
    raw = str(api_key or '').strip()
    if raw.startswith('tp-'):
        return 'token_plan'
    if raw.startswith('sk-'):
        return 'payg'
    return 'unknown'


def normalize_mimo_model_name(model_name: str) -> str:
    raw = str(model_name or '').strip()
    if raw.startswith('xiaomi/'):
        return raw.split('/', 1)[1].strip()
    if raw.startswith('mimo/'):
        return raw.split('/', 1)[1].strip()
    return raw


def _prefixed_native_model(model_name: str) -> str:
    normalized = normalize_mimo_model_name(model_name)
    return f'xiaomi/{normalized}' if normalized else ''


def _is_native_short_model(model_name: str) -> bool:
    return normalize_mimo_model_name(model_name).startswith('mimo-')


def is_native_mimo_model(model_name: str) -> bool:
    raw = str(model_name or '').strip().lower()
    normalized = normalize_mimo_model_name(raw)
    return raw.startswith(('xiaomi/', 'mimo/')) or normalized.startswith('mimo-')


def is_mimo_auto_router(model_name: str) -> bool:
    return normalize_mimo_model_name(model_name).lower() == 'mimo-auto'


def _dedupe_native_models(models: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in models:
        normalized = normalize_mimo_model_name(item)
        if not normalized.startswith('mimo-'):
            continue
        full_id = f'xiaomi/{normalized}'
        if full_id in seen:
            continue
        seen.add(full_id)
        ordered.append(full_id)
    return ordered


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _sanitize_name(model_id: str) -> str:
    sanitized = []
    for ch in str(model_id or '').strip():
        if ch.isalnum() or ch in {'-', '_', '.'}:
            sanitized.append(ch)
        else:
            sanitized.append('_')
    return ''.join(sanitized).strip('_') or 'model'


def _model_tier(model_id: str) -> str:
    lowered = normalize_mimo_model_name(model_id).lower()
    if 'flash' in lowered:
        return 'economy'
    if 'omni' in lowered:
        return 'multimodal'
    if any(token in lowered for token in ('2.5-pro', '-pro')):
        return 'frontier'
    if '2.5' in lowered:
        return 'standard_plus'
    return 'standard'


def _model_score(model_id: str, role: str) -> float:
    tier = _model_tier(model_id)
    base = {
        'frontier': 1.0,
        'multimodal': 0.9,
        'standard_plus': 0.84,
        'standard': 0.76,
        'economy': 0.66,
    }.get(tier, 0.7)
    if role == 'code_parallel':
        base += 0.14 if tier in {'frontier', 'standard_plus'} else 0.08
    elif role == 'review_primary':
        base += 0.12 if tier == 'frontier' else 0.06
    elif role == 'plan_primary':
        base += 0.11 if tier == 'frontier' else 0.05
    elif role == 'test_primary':
        base += 0.1 if tier in {'frontier', 'standard_plus'} else 0.05
    elif role == 'docs_primary':
        base += 0.08
    elif role == 'research_primary':
        base += 0.1 if tier == 'frontier' else 0.04
    if 'flash' in normalize_mimo_model_name(model_id).lower() and role in {'docs_primary', 'test_primary'}:
        base += 0.03
    return round(base, 4)


def _preferred_task_types(model_id: str) -> list[str]:
    tier = _model_tier(model_id)
    group = mimo_model_group(model_id)
    if group == 'asr':
        return ['speech_to_text', 'transcription', 'voice', 'analysis']
    if group == 'tts':
        return ['speech_synthesis', 'voice', 'audio', 'docs']
    if group == 'multimodal':
        return ['research', 'docs', 'plan', 'code', 'review', 'test']
    if tier == 'frontier':
        return ['code', 'review', 'plan', 'research', 'test', 'docs']
    if tier == 'economy':
        return ['docs', 'test', 'fix', 'code']
    return ['code', 'test', 'docs', 'fix', 'review', 'plan']


def _strengths(model_id: str) -> list[str]:
    lowered = normalize_mimo_model_name(model_id).lower()
    strengths: list[str] = ['coding', 'drafting']
    group = mimo_model_group(model_id)
    if 'pro' in lowered:
        strengths.append('deep_reasoning')
    if 'flash' in lowered:
        strengths.append('fast_turnaround')
    if group == 'multimodal':
        strengths.append('multimodal')
    if group == 'asr':
        strengths.append('speech_to_text')
    if group == 'tts':
        strengths.append('voice_synthesis')
    if '2.5' in lowered:
        strengths.append('review')
    return strengths


_MIMO_GROUP_DESCRIPTIONS: dict[str, str] = {
    'text': 'Text reasoning, coding, review, planning, and docs.',
    'multimodal': 'Mixed input reasoning and multimodal coordination.',
    'asr': 'Speech-to-text / transcription workloads that require audio input.',
    'tts': 'Text-to-speech / voice generation workloads that require assistant-style output payloads.',
}

_MIMO_GROUP_USE_CASES: dict[str, str] = {
    'text': 'General orchestrator work: coding, review, planning, docs, and synthesis.',
    'multimodal': 'Cross-modal tasks that combine text reasoning with media-aware prompts.',
    'asr': 'Audio transcription and speech-to-text ingestion.',
    'tts': 'Speech output, voice cloning, and voice design generation.',
}


def mimo_model_group(model_id: str) -> str:
    lowered = normalize_mimo_model_name(model_id).lower()
    if not lowered:
        return 'text'
    if 'asr' in lowered or 'transcribe' in lowered:
        return 'asr'
    if 'tts' in lowered or 'speech' in lowered or 'voiceclone' in lowered or 'voicedesign' in lowered:
        return 'tts'
    if 'omni' in lowered or 'multimodal' in lowered:
        return 'multimodal'
    return 'text'


def mimo_group_description(group: str) -> str:
    return _MIMO_GROUP_DESCRIPTIONS.get(str(group or '').strip().lower(), _MIMO_GROUP_DESCRIPTIONS['text'])


def mimo_group_use_case(group: str) -> str:
    return _MIMO_GROUP_USE_CASES.get(str(group or '').strip().lower(), _MIMO_GROUP_USE_CASES['text'])


def mimo_probe_mode_for_group(group: str) -> str:
    normalized = str(group or '').strip().lower()
    if normalized == 'asr':
        return 'input_audio'
    if normalized == 'tts':
        return 'assistant_text'
    if normalized == 'multimodal':
        return 'text_multimodal'
    return 'chat_text'


def mimo_model_subgroup(model_id: str) -> str:
    lowered = normalize_mimo_model_name(model_id).lower()
    group = mimo_model_group(model_id)
    if group == 'asr':
        return 'speech_to_text'
    if group == 'tts':
        if 'voiceclone' in lowered:
            return 'voice_clone'
        if 'voicedesign' in lowered:
            return 'voice_design'
        return 'speech_synthesis'
    if group == 'multimodal':
        return 'multimodal_reasoning'
    return 'text_reasoning'


def mimo_model_use_case(model_id: str) -> str:
    group = mimo_model_group(model_id)
    subgroup = mimo_model_subgroup(model_id)
    if group == 'asr':
        return 'Convert audio into text for transcripts, captions, and voice command ingestion.'
    if subgroup == 'voice_clone':
        return 'Clone a speaker voice from reference audio before generating speech output.'
    if subgroup == 'voice_design':
        return 'Design or refine a synthetic voice using text guidance.'
    if group == 'tts':
        return 'Generate speech from text prompts for narration, assistants, or playback.'
    if group == 'multimodal':
        return 'Combine text reasoning with richer multimodal context when the task needs it.'
    return 'Handle text-first orchestration tasks such as coding, review, planning, and docs.'


def build_mimo_model_profile(model_id: str) -> dict[str, Any]:
    tier = _model_tier(model_id)
    thresholds = {
        'frontier': {'success_rate': 0.9, 'avg_latency_ms': 2200, 'quality_min_confidence': 0.9},
        'multimodal': {'success_rate': 0.87, 'avg_latency_ms': 2400, 'quality_min_confidence': 0.86},
        'standard_plus': {'success_rate': 0.86, 'avg_latency_ms': 1900, 'quality_min_confidence': 0.84},
        'standard': {'success_rate': 0.84, 'avg_latency_ms': 1700, 'quality_min_confidence': 0.8},
        'economy': {'success_rate': 0.8, 'avg_latency_ms': 1400, 'quality_min_confidence': 0.76},
    }[tier]
    context_depth = {'frontier': 6, 'multimodal': 5, 'standard_plus': 5, 'standard': 4, 'economy': 3}[tier]
    model_key = _prefixed_native_model(model_id)
    group = mimo_model_group(model_id)
    metadata = {
        'model_family': 'mimo',
        'provider_family': 'xiaomi_mimo_native',
        'native_direct': True,
        'generated': True,
        'tier': tier,
        'mimo_group': group,
        'mimo_subgroup': mimo_model_subgroup(model_id),
        'probe_mode': mimo_probe_mode_for_group(group),
        'group_description': mimo_group_description(group),
        'group_use_case': mimo_group_use_case(group),
        'model_use_case': mimo_model_use_case(model_id),
    }
    model_payload = {
        'profile_type': 'model',
        'profile_key': f'model::{model_key}',
        'provider_weights': {
            'mimo': {'quality': 1.16, 'budget': 0.92, 'vfs': 0.98},
            'local': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
            'openai': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
            'mistral': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
        },
        'model_class_weights': {'mimo': {'quality': 1.1, 'budget': 0.94}},
        'thresholds': thresholds,
        'default_context_depth': context_depth,
        'budget_pressure': {'high': 2200, 'medium': 4400},
        'quality_pressure': {'high': 0.9, 'medium': 0.82},
        'metadata': metadata,
    }
    combo_payload = {
        'profile_type': 'combo',
        'profile_key': f'combo::mimo::{model_key}',
        'provider_weights': {
            'mimo': {'quality': 1.2, 'budget': 0.9, 'vfs': 0.96},
            'local': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
            'openai': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
            'mistral': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
        },
        'model_class_weights': {'mimo': {'quality': 1.12, 'budget': 0.92}},
        'thresholds': {
            'success_rate': round(float(thresholds['success_rate']) + 0.01, 2),
            'avg_latency_ms': max(1200, int(thresholds['avg_latency_ms']) - 60),
            'quality_min_confidence': round(min(0.98, float(thresholds['quality_min_confidence']) + 0.02), 2),
        },
        'default_context_depth': max(context_depth, 4),
        'budget_pressure': {'high': 2400, 'medium': 4800},
        'quality_pressure': {'high': 0.92, 'medium': 0.84},
        'metadata': metadata,
    }
    return {'model': model_payload, 'combo': combo_payload}


def build_mimo_orchestrator_templates(models: list[str], *, base_url: str = '') -> dict[str, Any]:
    compatible = _dedupe_native_models(models)
    roles = ('code_parallel', 'review_primary', 'plan_primary', 'test_primary', 'docs_primary', 'research_primary')
    templates_by_role: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        rows: list[dict[str, Any]] = []
        for model in compatible:
            group = mimo_model_group(model)
            rows.append({
                'role': role,
                'provider': 'mimo',
                'model_name': model,
                'family': 'mimo',
                'tier': _model_tier(model),
                'group': group,
                'group_description': mimo_group_description(group),
                'group_use_case': mimo_group_use_case(group),
                'subgroup': mimo_model_subgroup(model),
                'model_use_case': mimo_model_use_case(model),
                'probe_mode': mimo_probe_mode_for_group(group),
                'preferred_task_types': _preferred_task_types(model),
                'strengths': _strengths(model),
                'score': _model_score(model, role),
            })
        rows.sort(key=lambda row: (-float(row['score']), str(row['model_name'])))
        templates_by_role[role] = rows[:8]
    return {
        'generated_at': int(time.time()),
        'provider': 'mimo',
        'base_url': base_url,
        'template_count': sum(len(items) for items in templates_by_role.values()),
        'roles': templates_by_role,
        'defaults': {
            'code_parallel_branch_count': min(4, len(templates_by_role.get('code_parallel', []))),
            'review_model': (templates_by_role.get('review_primary') or [{}])[0].get('model_name', ''),
            'planning_model': (templates_by_role.get('plan_primary') or [{}])[0].get('model_name', ''),
        },
    }


def resolve_mimo_provider_config() -> MimoProviderConfig:
    api_key = _first_env('MIMO_API_KEY', 'AI_BRIDGE_MIMO_API_KEY')
    explicit_base_url = _normalize_url(_first_env('MIMO_BASE_URL', 'AI_BRIDGE_MIMO_BASE_URL'))
    base_url = explicit_base_url or 'https://api.xiaomimimo.com/v1'
    models_endpoint = _normalize_url(_first_env('MIMO_MODELS_ENDPOINT', 'AI_BRIDGE_MIMO_MODELS_ENDPOINT')) or _join_url(base_url, 'models')
    chat_endpoint = _normalize_url(_first_env('MIMO_CHAT_COMPLETIONS_ENDPOINT', 'AI_BRIDGE_MIMO_CHAT_COMPLETIONS_ENDPOINT')) or _join_url(base_url, 'chat/completions')
    default_model = _first_env('AI_BRIDGE_MIMO_DEFAULT_MODEL', 'MIMO_DEFAULT_MODEL') or 'xiaomi/mimo-v2.5-pro'
    return MimoProviderConfig(api_key=api_key, base_url=base_url, models_endpoint=models_endpoint, chat_completions_endpoint=chat_endpoint, default_model=default_model, key_kind=mimo_key_kind(api_key), base_url_explicit=bool(explicit_base_url))


def mimo_endpoint_manifest(config: MimoProviderConfig | None = None) -> dict[str, Any]:
    cfg = config or resolve_mimo_provider_config()
    return {
        'provider': 'mimo',
        'base_url': cfg.base_url,
        'default_model': _prefixed_native_model(cfg.default_model) or 'xiaomi/mimo-v2.5-pro',
        'endpoints': {
            'models': cfg.models_endpoint,
            'chat_completions': cfg.chat_completions_endpoint,
        },
        'endpoint_roles': {
            'inventory': 'models',
            'chat': 'chat_completions',
        },
    }


def _generated_native_mimo_models() -> list[str]:
    generated_root = _mimo_generated_profile_root()
    manifest_path = generated_root / 'manifest.json'
    models: list[str] = []
    manifest = _read_json(manifest_path)
    manifest_models = manifest.get('models') if isinstance(manifest, dict) else []
    if isinstance(manifest_models, list):
        models.extend(str(item).strip() for item in manifest_models if str(item).strip())
    model_profiles = manifest.get('model_profiles') if isinstance(manifest, dict) else []
    if isinstance(model_profiles, list):
        for rel_path in model_profiles:
            profile_path = generated_root / str(rel_path)
            profile = _read_json(profile_path)
            profile_key = str(profile.get('profile_key') or '').strip()
            metadata = profile.get('metadata') if isinstance(profile.get('metadata'), dict) else {}
            if not profile_key.startswith('model::'):
                continue
            model_name = profile_key.split('model::', 1)[1].strip()
            family = str(metadata.get('model_family') or '').strip().lower()
            if model_name and family == 'mimo':
                models.append(model_name)
    return _dedupe_native_models(models)


def load_mimo_model_catalog() -> dict[str, Any]:
    payload = _read_json(_mimo_full_cache_path())
    if payload:
        payload.setdefault('models', [])
        payload.setdefault('source', 'cache')
        payload.setdefault('ok', bool(payload.get('models')))
        if payload.get('models'):
            return payload
    short_cache = _read_json(_mimo_cache_path())
    short_models = _dedupe_native_models(list(short_cache.get('models') or [])) if short_cache else []
    if short_models:
        return {
            'ts': int(short_cache.get('ts') or time.time()) if isinstance(short_cache, dict) else int(time.time()),
            'provider': 'mimo',
            'base_url': resolve_mimo_provider_config().base_url,
            'endpoint': resolve_mimo_provider_config().models_endpoint,
            'status_code': short_cache.get('status_code') if isinstance(short_cache, dict) else None,
            'source': 'short_cache_fallback',
            'error': None,
            'total_models': len(short_models),
            'models': short_models,
        }
    return payload


def fetch_mimo_model_catalog(*, force_refresh: bool = False, timeout_sec: float = 20.0) -> dict[str, Any]:
    cfg = resolve_mimo_provider_config()
    cache_path = _mimo_full_cache_path()
    cached = _read_json(cache_path)
    cached_models = _dedupe_native_models(list(cached.get('models') or [])) if cached else []
    cached_ts = int(cached.get('ts') or 0) if cached else 0
    if cached_models and not force_refresh and (time.time() - cached_ts) < _mimo_cache_ttl_sec():
        return {
            'ok': True,
            'source': 'cache',
            'provider': 'mimo',
            'base_url': cfg.base_url,
            'endpoint': cfg.models_endpoint,
            'status_code': int(cached.get('status_code') or 200),
            'models': cached_models,
            'model_count': len(cached_models),
            'error': None,
            'generated_at': cached_ts,
        }
    if not cfg.api_key:
        generated = _generated_native_mimo_models()
        return {
            'ok': bool(generated),
            'source': 'generated_manifest' if generated else 'unconfigured',
            'provider': 'mimo',
            'base_url': cfg.base_url,
            'endpoint': cfg.models_endpoint,
            'status_code': None,
            'models': generated,
            'model_count': len(generated),
            'error': None if generated else 'MIMO_API_KEY not set',
            'generated_at': cached_ts or None,
        }
    if cfg.key_kind == 'token_plan' and not cfg.base_url_explicit:
        return {
            'ok': False,
            'source': 'preflight',
            'provider': 'mimo',
            'base_url': cfg.base_url,
            'endpoint': cfg.models_endpoint,
            'status_code': None,
            'models': cached_models,
            'model_count': len(cached_models),
            'error': 'Token Plan key detected (tp-...), but MIMO_BASE_URL/AI_BRIDGE_MIMO_BASE_URL is not configured',
            'generated_at': cached_ts or None,
        }
    try:
        response = httpx.get(cfg.models_endpoint, headers=mimo_request_headers(cfg.api_key), timeout=timeout_sec)
    except Exception as exc:
        fallback = cached_models or _generated_native_mimo_models()
        return {
            'ok': bool(fallback),
            'source': 'cache_fallback' if fallback else 'network_error',
            'provider': 'mimo',
            'base_url': cfg.base_url,
            'endpoint': cfg.models_endpoint,
            'status_code': None,
            'models': fallback,
            'model_count': len(fallback),
            'error': str(exc),
            'generated_at': cached_ts or None,
        }
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    rows = payload.get('data') if isinstance(payload, dict) else []
    live_models: list[str] = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get('id') or '').strip()
        if _is_native_short_model(model_id):
            live_models.append(_prefixed_native_model(model_id))
    live_models = _dedupe_native_models(live_models)
    if response.status_code >= 400:
        message = ''
        if isinstance(payload, dict):
            error_obj = payload.get('error') or {}
            if isinstance(error_obj, dict):
                message = str(error_obj.get('message') or '').strip()
        message = message or str(response.text or f'http_{response.status_code}').strip()
        fallback = cached_models or _generated_native_mimo_models()
        return {
            'ok': bool(fallback),
            'source': 'cache_fallback' if fallback else 'live_http_error',
            'provider': 'mimo',
            'base_url': cfg.base_url,
            'endpoint': cfg.models_endpoint,
            'status_code': int(response.status_code),
            'models': fallback,
            'model_count': len(fallback),
            'error': message,
            'generated_at': cached_ts or None,
        }
    ts = int(time.time())
    cache_payload = {
        'ts': ts,
        'provider': 'mimo',
        'base_url': cfg.base_url,
        'endpoint': cfg.models_endpoint,
        'status_code': int(response.status_code),
        'model_count': len(live_models),
        'models': live_models,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache_payload, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    _mimo_cache_path().parent.mkdir(parents=True, exist_ok=True)
    _mimo_cache_path().write_text(json.dumps({'ts': ts, 'models': live_models}, ensure_ascii=True) + '\n', encoding='utf-8')
    return {
        'ok': True,
        'source': 'live',
        'provider': 'mimo',
        'base_url': cfg.base_url,
        'endpoint': cfg.models_endpoint,
        'status_code': int(response.status_code),
        'models': live_models,
        'model_count': len(live_models),
        'error': None,
        'generated_at': ts,
    }


def configured_native_mimo_models() -> list[str]:
    catalog = load_mimo_model_catalog()
    if catalog.get('models'):
        return _dedupe_native_models(list(catalog.get('models') or []))
    generated = _generated_native_mimo_models()
    if generated:
        return generated
    default_model = _first_env('AI_BRIDGE_MIMO_DEFAULT_MODEL', 'MIMO_DEFAULT_MODEL') or 'xiaomi/mimo-v2.5-pro'
    candidates = [
        default_model,
        'xiaomi/mimo-v2.5-pro',
        'xiaomi/mimo-v2.5',
        'xiaomi/mimo-v2-pro',
        'xiaomi/mimo-v2-omni',
        'xiaomi/mimo-v2-flash',
    ]
    return _dedupe_native_models([item for item in candidates if is_native_mimo_model(item) and not is_mimo_auto_router(item)])


def sync_mimo_native_artifacts(models: list[str] | None = None, *, force_refresh: bool = False, timeout_sec: float = 20.0) -> dict[str, Any]:
    cfg = resolve_mimo_provider_config()
    if models is None:
        catalog = fetch_mimo_model_catalog(force_refresh=force_refresh, timeout_sec=timeout_sec)
        native_models = _dedupe_native_models(list(catalog.get('models') or []))
    else:
        native_models = _dedupe_native_models(models)
        catalog = {
            'ok': bool(native_models),
            'source': 'supplied',
            'provider': 'mimo',
            'base_url': cfg.base_url,
            'endpoint': cfg.models_endpoint,
            'status_code': None,
            'models': native_models,
            'model_count': len(native_models),
            'error': None,
            'generated_at': int(time.time()),
        }
    generated_root = _mimo_generated_profile_root()
    templates_path = _mimo_templates_path(generated_root)
    models_dir = generated_root / 'models'
    combos_dir = generated_root / 'combinations'
    models_dir.mkdir(parents=True, exist_ok=True)
    combos_dir.mkdir(parents=True, exist_ok=True)
    generated_model_files: list[str] = []
    generated_combo_files: list[str] = []
    for model in native_models:
        payload = build_mimo_model_profile(model)
        safe = _sanitize_name(normalize_mimo_model_name(model))
        model_name = f'model__{safe}.json'
        combo_name = f'combo__mimo__{safe}.json'
        (models_dir / model_name).write_text(json.dumps(payload['model'], ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        (combos_dir / combo_name).write_text(json.dumps(payload['combo'], ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        generated_model_files.append(str(Path('models') / model_name))
        generated_combo_files.append(str(Path('combinations') / combo_name))
    template_payload = build_mimo_orchestrator_templates(native_models, base_url=cfg.base_url)
    templates_path.parent.mkdir(parents=True, exist_ok=True)
    templates_path.write_text(json.dumps(template_payload, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    ts = int(time.time())
    manifest = {
        'generated_at': ts,
        'provider': 'mimo',
        'base_url': cfg.base_url,
        'models': native_models,
        'model_profiles': generated_model_files,
        'combo_profiles': generated_combo_files,
        'orchestrator_templates': str(templates_path.relative_to(generated_root)) if templates_path.is_relative_to(generated_root) else str(templates_path),
        'endpoint_manifest': mimo_endpoint_manifest(cfg),
        'inventory_source': {
            'source': str(catalog.get('source') or 'unknown'),
            'endpoint': str(catalog.get('endpoint') or cfg.models_endpoint),
            'status_code': catalog.get('status_code'),
            'error': catalog.get('error'),
        },
    }
    (generated_root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    full_payload = {
        'ts': ts,
        'provider': 'mimo',
        'base_url': cfg.base_url,
        'endpoint': cfg.models_endpoint,
        'status_code': catalog.get('status_code'),
        'source': catalog.get('source'),
        'error': catalog.get('error'),
        'total_models': len(native_models),
        'models': native_models,
        'generated_profile_root': str(generated_root),
        'orchestrator_templates_path': str(templates_path),
        'endpoint_manifest': mimo_endpoint_manifest(cfg),
    }
    full_cache_path = _mimo_full_cache_path()
    full_cache_path.parent.mkdir(parents=True, exist_ok=True)
    full_cache_path.write_text(json.dumps(full_payload, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    return {
        'ok': bool(native_models),
        'source': str(catalog.get('source') or 'unknown'),
        'cache_path': str(_mimo_cache_path()),
        'full_cache_path': str(full_cache_path),
        'generated_profile_root': str(generated_root),
        'orchestrator_templates_path': str(templates_path),
        'total_models': len(native_models),
        'status_code': catalog.get('status_code'),
        'endpoint': str(catalog.get('endpoint') or cfg.models_endpoint),
        'error': catalog.get('error'),
    }


def mimo_request_headers(api_key: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {api_key}', 'api-key': api_key, 'Content-Type': 'application/json'}


def preflight_mimo_native_request(model_name: str, config: MimoProviderConfig | None = None) -> str | None:
    cfg = config or resolve_mimo_provider_config()
    if not cfg.api_key:
        return 'MIMO_API_KEY not set'
    if is_mimo_auto_router(model_name):
        return 'mimo-auto is not a direct Xiaomi API model; use xiaomi/mimo-v2.5-pro or another xiaomi/mimo-* model'
    if cfg.key_kind == 'token_plan' and not cfg.base_url_explicit:
        return 'Token Plan key detected (tp-...), but MIMO_BASE_URL/AI_BRIDGE_MIMO_BASE_URL is not configured'
    normalized_model = normalize_mimo_model_name(model_name)
    if not normalized_model or not normalized_model.startswith('mimo-'):
        return f'unsupported_native_mimo_model:{model_name}'
    return None


def extract_mimo_response_text(payload: Any) -> str:
    return extract_provider_response_text(payload)


def invoke_mimo_native(model_name: str, prompt: str, *, timeout_sec: float = 45.0, max_completion_tokens: int = 1200, temperature: float = 0.2) -> tuple[dict[str, Any] | None, str | None, int | None]:
    cfg = resolve_mimo_provider_config()
    preflight_error = preflight_mimo_native_request(model_name, cfg)
    if preflight_error:
        return None, preflight_error, None
    if not has_meaningful_request_payload(prompt):
        return None, EMPTY_PROVIDER_REQUEST_ERROR, None
    normalized_model = normalize_mimo_model_name(model_name)
    try:
        response = httpx.post(
            cfg.chat_completions_endpoint,
            headers=mimo_request_headers(cfg.api_key),
            json={
                'model': normalized_model,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_completion_tokens': max_completion_tokens,
                'temperature': temperature,
                'stream': False,
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
            err = payload.get('error') or {}
            message = str(err.get('message') or response.text or f'http_{response.status_code}').strip()
            param = str(err.get('param') or '').strip()
            if param:
                message = f'{message}: {param}'
            return payload, message, response.status_code
        return None, (response.text or f'http_{response.status_code}').strip(), response.status_code
    if isinstance(payload, dict) and not provider_response_has_assistant_content_or_tool_calls(payload):
        return payload, EMPTY_ASSISTANT_RESPONSE_ERROR, response.status_code
    return payload if isinstance(payload, dict) else None, None, response.status_code


def _mimo_silent_wav_base64(*, duration_ms: int = 250, sample_rate: int = 16000) -> str:
    frame_count = max(1, int(sample_rate * max(1, duration_ms) / 1000))
    pcm = b"\x00\x00" * frame_count
    with io.BytesIO() as buffer:
        with wave.open(buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        return base64.b64encode(buffer.getvalue()).decode('ascii')


def _mimo_silent_wav_data_url(*, duration_ms: int = 250, sample_rate: int = 16000) -> str:
    return f'data:audio/wav;base64,{_mimo_silent_wav_base64(duration_ms=duration_ms, sample_rate=sample_rate)}'


def _mimo_input_audio_payload(*, duration_ms: int = 250, sample_rate: int = 16000) -> dict[str, Any]:
    return {
        'data': _mimo_silent_wav_data_url(duration_ms=duration_ms, sample_rate=sample_rate),
        'format': 'wav',
    }


def _mimo_audio_payload(*, duration_ms: int = 250, sample_rate: int = 16000) -> dict[str, Any]:
    return {
        'data': _mimo_silent_wav_data_url(duration_ms=duration_ms, sample_rate=sample_rate),
        'format': 'wav',
        'voice': _mimo_silent_wav_data_url(duration_ms=max(120, duration_ms), sample_rate=sample_rate),
    }

def build_mimo_probe_payload(model_name: str, prompt: str, *, group: str | None = None) -> dict[str, Any]:
    normalized_model = normalize_mimo_model_name(model_name)
    model_group = str(group or mimo_model_group(model_name)).strip().lower()
    if model_group == 'asr':
        return {
            'model': normalized_model,
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'input_audio',
                            'input_audio': _mimo_input_audio_payload(),
                        }
                    ],
                }
            ],
            'max_completion_tokens': 64,
            'temperature': 0.0,
            'stream': False,
        }
    if 'voiceclone' in normalized_model.lower():
        return {
            'model': normalized_model,
            'messages': [
                {
                    'role': 'assistant',
                    'content': prompt,
                }
            ],
            'audio': _mimo_audio_payload(),
            'max_completion_tokens': 64,
            'temperature': 0.0,
            'stream': False,
        }
    if 'voicedesign' in normalized_model.lower():
        return {
            'model': normalized_model,
            'messages': [
                {
                    'role': 'user',
                    'content': prompt,
                }
            ],
            'max_completion_tokens': 64,
            'temperature': 0.0,
            'stream': False,
        }
    if model_group == 'tts':
        return {
            'model': normalized_model,
            'messages': [
                {
                    'role': 'assistant',
                    'content': prompt,
                }
            ],
            'max_completion_tokens': 64,
            'temperature': 0.0,
            'stream': False,
        }
    return {
        'model': normalized_model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_completion_tokens': 64,
        'temperature': 0.0,
        'stream': False,
    }


def invoke_mimo_group_probe(model_name: str, prompt: str, *, group: str | None = None, timeout_sec: float = 45.0) -> tuple[dict[str, Any] | None, str | None, int | None, str]:
    cfg = resolve_mimo_provider_config()
    preflight_error = preflight_mimo_native_request(model_name, cfg)
    probe_group = str(group or mimo_model_group(model_name)).strip().lower()
    if preflight_error:
        return None, preflight_error, None, probe_group
    if not has_meaningful_request_payload(prompt):
        return None, EMPTY_PROVIDER_REQUEST_ERROR, None, probe_group
    try:
        response = httpx.post(
            cfg.chat_completions_endpoint,
            headers=mimo_request_headers(cfg.api_key),
            json=build_mimo_probe_payload(model_name, prompt, group=probe_group),
            timeout=timeout_sec,
        )
    except Exception as exc:
        return None, str(exc), None, probe_group
    try:
        payload = response.json()
    except Exception:
        payload = None
    if response.status_code >= 400:
        if isinstance(payload, dict):
            err = payload.get('error') or {}
            message = str(err.get('message') or response.text or f'http_{response.status_code}').strip()
            param = str(err.get('param') or '').strip()
            if param:
                message = f'{message}: {param}'
            return payload, message, response.status_code, probe_group
        return None, (response.text or f'http_{response.status_code}').strip(), response.status_code, probe_group
    if isinstance(payload, dict) and not provider_response_has_assistant_content_or_tool_calls(payload):
        return payload, EMPTY_ASSISTANT_RESPONSE_ERROR, response.status_code, probe_group
    return payload if isinstance(payload, dict) else None, None, response.status_code, probe_group
