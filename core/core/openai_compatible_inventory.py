from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .openai_provider import openai_endpoint_manifest, resolve_openai_provider_config


_NON_TEXT_MARKERS = (
    'embedding',
    'moderation',
    'tts',
    'whisper',
    'image',
    'sora',
    'dall',
    'realtime',
    'audio',
    'transcribe',
    'speech',
)

_RUNTIME_BLOCK_MARKERS = (
    'unsupported model',
    'model is not supported',
    'invalid model',
    'does not exist',
    'not found',
    'no eligible resources',
)


def is_text_compatible_model(model_id: str) -> bool:
    lowered = str(model_id or '').strip().lower()
    if not lowered:
        return False
    return not any(token in lowered for token in _NON_TEXT_MARKERS)


def is_openai_family_model(model_id: str) -> bool:
    lowered = str(model_id or '').strip().lower()
    return lowered.startswith(('gpt-', 'o', 'codex')) or 'codex' in lowered


def _family_name(model_id: str) -> str:
    lowered = str(model_id or '').strip().lower()
    for token, name in (
        ('gpt', 'gpt'),
        ('claude', 'claude'),
        ('deepseek', 'deepseek'),
        ('qwen', 'qwen'),
        ('kimi', 'kimi'),
        ('glm', 'glm'),
        ('minimax', 'minimax'),
        ('mimo', 'mimo'),
        ('hy3', 'hy3'),
    ):
        if token in lowered:
            return name
    return 'generic'


def _family_rank(model_id: str) -> tuple[int, str]:
    lowered = str(model_id or '').strip().lower()
    if lowered.startswith('gpt-5.5'):
        return (0, lowered)
    if lowered.startswith('gpt-5.4'):
        return (1, lowered)
    if lowered.startswith('claude-sonnet'):
        return (2, lowered)
    if lowered.startswith('claude-opus'):
        return (3, lowered)
    if lowered.startswith('claude-haiku'):
        return (4, lowered)
    if lowered.startswith('deepseek'):
        return (5, lowered)
    if lowered.startswith('qwen'):
        return (6, lowered)
    if lowered.startswith('kimi'):
        return (7, lowered)
    if lowered.startswith('glm'):
        return (8, lowered)
    if lowered.startswith('mimo'):
        return (9, lowered)
    if lowered.startswith('minimax'):
        return (10, lowered)
    if lowered.startswith('hy3'):
        return (11, lowered)
    return (12, lowered)


def _sanitize_name(model_id: str) -> str:
    sanitized = []
    for ch in str(model_id or '').strip():
        if ch.isalnum() or ch in {'-', '_', '.'}:
            sanitized.append(ch)
        else:
            sanitized.append('_')
    return ''.join(sanitized).strip('_') or 'model'


def _class_weights() -> dict[str, dict[str, float]]:
    return {
        'gpt': {'quality': 1.18, 'budget': 0.9},
        'claude': {'quality': 1.2, 'budget': 0.88},
        'deepseek': {'quality': 1.12, 'budget': 0.93},
        'qwen': {'quality': 1.08, 'budget': 0.95},
        'kimi': {'quality': 1.08, 'budget': 0.94},
        'glm': {'quality': 1.05, 'budget': 0.96},
        'minimax': {'quality': 1.04, 'budget': 0.97},
        'mimo': {'quality': 1.1, 'budget': 0.94},
        'hy3': {'quality': 1.03, 'budget': 0.98},
    }


def _profile_tier(model_id: str) -> str:
    lowered = str(model_id or '').strip().lower()
    if any(token in lowered for token in ('nano', 'haiku', 'flash')):
        return 'economy'
    if 'mini' in lowered:
        return 'mini'
    if any(token in lowered for token in ('5.5', 'opus', 'sonnet-4-6', 'pro', 'max')):
        return 'frontier'
    return 'standard'


def _thresholds_for_tier(tier: str) -> tuple[dict[str, float], int, dict[str, int], dict[str, float], float, float]:
    if tier == 'economy':
        return ({'success_rate': 0.8, 'avg_latency_ms': 1400, 'quality_min_confidence': 0.76}, 3, {'high': 1200, 'medium': 2400}, {'high': 0.84, 'medium': 0.74}, 1.06, 1.08)
    if tier == 'mini':
        return ({'success_rate': 0.83, 'avg_latency_ms': 1550, 'quality_min_confidence': 0.8}, 4, {'high': 1600, 'medium': 3200}, {'high': 0.87, 'medium': 0.78}, 1.12, 1.14)
    if tier == 'frontier':
        return ({'success_rate': 0.9, 'avg_latency_ms': 2100, 'quality_min_confidence': 0.9}, 6, {'high': 2800, 'medium': 5600}, {'high': 0.95, 'medium': 0.88}, 1.3, 1.34)
    return ({'success_rate': 0.86, 'avg_latency_ms': 1850, 'quality_min_confidence': 0.84}, 5, {'high': 2200, 'medium': 4400}, {'high': 0.9, 'medium': 0.82}, 1.18, 1.22)


def build_model_profile(model_id: str) -> dict[str, object]:
    tier = _profile_tier(model_id)
    thresholds, context_depth, budget_pressure, quality_pressure, provider_quality, combo_quality = _thresholds_for_tier(tier)
    family = _family_name(model_id)
    class_weights = _class_weights()
    provider_budget = 0.82 if tier == 'frontier' else (0.9 if tier == 'standard' else 0.96)
    combo_budget = max(0.78, provider_budget - 0.02)
    family_bonus = class_weights.get(family, {'quality': 1.02, 'budget': 0.99})
    model_payload = {
        'profile_type': 'model',
        'profile_key': f'model::{model_id}',
        'provider_weights': {
            'openai': {'quality': round(provider_quality, 2), 'budget': round(provider_budget, 2), 'vfs': 0.98},
            'local': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
            'mistral': {'quality': 1.04, 'budget': 0.98, 'vfs': 1.0},
        },
        'model_class_weights': class_weights,
        'thresholds': thresholds,
        'default_context_depth': context_depth,
        'budget_pressure': budget_pressure,
        'quality_pressure': quality_pressure,
        'metadata': {'model_family': family, 'generated': True, 'tier': tier},
    }
    combo_payload = {
        'profile_type': 'combo',
        'profile_key': f'combo::openai::{model_id}',
        'provider_weights': {
            'openai': {'quality': round(combo_quality, 2), 'budget': round(combo_budget, 2), 'vfs': 0.96},
            'local': {'quality': 1.0, 'budget': 1.0, 'vfs': 1.0},
            'mistral': {'quality': 1.05, 'budget': 0.98, 'vfs': 1.0},
        },
        'model_class_weights': class_weights,
        'thresholds': {
            'success_rate': round(float(thresholds['success_rate']) + 0.01, 2),
            'avg_latency_ms': max(1200, int(thresholds['avg_latency_ms']) - 50),
            'quality_min_confidence': round(min(0.98, float(thresholds['quality_min_confidence']) + 0.02), 2),
        },
        'default_context_depth': max(context_depth, 4),
        'budget_pressure': {'high': int(budget_pressure['high'] * 1.08), 'medium': int(budget_pressure['medium'] * 1.05)},
        'quality_pressure': {'high': round(min(0.98, float(quality_pressure['high']) + 0.01), 2), 'medium': round(min(0.95, float(quality_pressure['medium']) + 0.02), 2)},
        'metadata': {'model_family': family, 'generated': True, 'tier': tier},
    }
    model_payload['metadata']['class_weights'] = family_bonus
    combo_payload['metadata']['class_weights'] = family_bonus
    return {'model': model_payload, 'combo': combo_payload}


def _template_preferred_task_types(model_id: str) -> list[str]:
    family = _family_name(model_id)
    tier = _profile_tier(model_id)
    if tier == 'frontier':
        base = ['code', 'review', 'plan', 'research']
    elif tier == 'mini':
        base = ['code', 'docs', 'test', 'fix']
    elif tier == 'economy':
        base = ['docs', 'test', 'fix', 'code']
    else:
        base = ['code', 'review', 'test', 'docs']
    if family in {'qwen', 'deepseek'} and 'code' in base:
        base = ['code', 'test', 'fix', *[item for item in base if item not in {'code', 'test', 'fix'}]]
    if family == 'claude':
        base = ['review', 'plan', 'code', 'research', *[item for item in base if item not in {'review', 'plan', 'code', 'research'}]]
    if family == 'gpt':
        base = ['code', 'review', 'plan', 'docs', *[item for item in base if item not in {'code', 'review', 'plan', 'docs'}]]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in base:
        if item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered


def _template_strengths(model_id: str) -> list[str]:
    lowered = str(model_id or '').strip().lower()
    strengths: list[str] = []
    if any(token in lowered for token in ('opus', '5.5', 'max', 'pro')):
        strengths.append('deep_reasoning')
    if any(token in lowered for token in ('mini', 'haiku', 'flash')):
        strengths.append('fast_turnaround')
    if any(token in lowered for token in ('qwen', 'deepseek', 'gpt', 'claude')):
        strengths.append('coding')
    if any(token in lowered for token in ('claude', 'gpt', 'glm')):
        strengths.append('review')
    if 'mimo' in lowered:
        strengths.append('drafting')
    if 'qwen' in lowered or 'deepseek' in lowered:
        strengths.append('test_generation')
    return strengths or ['general']


def _template_score(model_id: str, role: str) -> float:
    tier = _profile_tier(model_id)
    family = _family_name(model_id)
    base = {'frontier': 1.0, 'standard': 0.76, 'mini': 0.66, 'economy': 0.58}.get(tier, 0.5)
    if role == 'code_parallel':
        base += {'gpt': 0.22, 'claude': 0.18, 'deepseek': 0.17, 'qwen': 0.15, 'kimi': 0.1}.get(family, 0.08)
    elif role == 'review_primary':
        base += {'claude': 0.24, 'gpt': 0.2, 'deepseek': 0.12, 'glm': 0.1}.get(family, 0.06)
    elif role == 'plan_primary':
        base += {'claude': 0.22, 'gpt': 0.18, 'kimi': 0.12, 'glm': 0.1}.get(family, 0.06)
    elif role == 'test_primary':
        base += {'deepseek': 0.18, 'qwen': 0.17, 'gpt': 0.14, 'claude': 0.1}.get(family, 0.05)
    elif role == 'docs_primary':
        base += {'gpt': 0.18, 'claude': 0.16, 'glm': 0.1, 'mimo': 0.1}.get(family, 0.05)
    elif role == 'research_primary':
        base += {'claude': 0.22, 'gpt': 0.2, 'kimi': 0.14, 'minimax': 0.1}.get(family, 0.06)
    if 'flash' in model_id.lower() or 'haiku' in model_id.lower():
        base += 0.04 if role in {'docs_primary', 'test_primary'} else -0.02
    if 'mini' in model_id.lower():
        base += 0.03 if role in {'docs_primary', 'code_parallel'} else 0.0
    return round(base, 4)


def _validated_row_map(validated_rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in validated_rows or []:
        if not isinstance(row, dict):
            continue
        model_name = str(row.get('model') or '').strip()
        if model_name:
            mapped[model_name] = row
    return mapped


def _endpoint_ok(row: dict[str, Any], key: str) -> bool:
    payload = row.get(key) if isinstance(row, dict) else {}
    return bool((payload or {}).get('ok')) if isinstance(payload, dict) else False


def _endpoint_error(row: dict[str, Any], key: str) -> str:
    payload = row.get(key) if isinstance(row, dict) else {}
    if not isinstance(payload, dict):
        return ''
    return str(payload.get('error') or payload.get('response_sample') or '').strip().lower()


def _runtime_status_for_model(model_id: str, row: dict[str, Any] | None) -> str:
    if not is_text_compatible_model(model_id):
        return 'non_chat_incompatible'
    if not row:
        return 'discovered'
    chat_ok = _endpoint_ok(row, 'chat_completions')
    responses_ok = _endpoint_ok(row, 'responses')
    if chat_ok and responses_ok:
        return 'routable'
    combined_error = ' '.join(filter(None, (_endpoint_error(row, 'chat_completions'), _endpoint_error(row, 'responses'))))
    if any(marker in combined_error for marker in _RUNTIME_BLOCK_MARKERS):
        return 'blocked'
    if chat_ok:
        return 'chat_only'
    if responses_ok:
        return 'responses_only'
    return 'probe_failed'


def _template_availability_rank(status: str) -> int:
    return {
        'routable': 0,
        'chat_only': 1,
        'responses_only': 1,
        'discovered': 2,
        'probe_failed': 3,
        'blocked': 4,
        'non_chat_incompatible': 5,
    }.get(str(status or ''), 6)


def build_runtime_model_template_manifest(
    models: list[str],
    *,
    validated_rows: list[dict[str, Any]] | None = None,
    base_url: str = '',
    default_model: str = '',
) -> dict[str, Any]:
    compatible = sorted({str(model).strip() for model in models if str(model).strip()}, key=_family_rank)
    validated_map = _validated_row_map(validated_rows)
    roles = ('code_parallel', 'review_primary', 'plan_primary', 'test_primary', 'docs_primary', 'research_primary')
    rows: list[dict[str, Any]] = []
    for model in compatible:
        status = _runtime_status_for_model(model, validated_map.get(model))
        role_scores = {role: _template_score(model, role) for role in roles}
        recommended_roles = [name for name, _ in sorted(role_scores.items(), key=lambda item: (-float(item[1]), item[0]))[:3]]
        row = validated_map.get(model) or {}
        rows.append({
            'model_name': model,
            'family': _family_name(model),
            'tier': _profile_tier(model),
            'status': status,
            'discovered': True,
            'chat_compatible': is_text_compatible_model(model),
            'default_candidate': model == str(default_model or '').strip(),
            'kernel_eligible': status == 'routable',
            'fallback_candidate': status in {'routable', 'discovered', 'chat_only'},
            'preferred_task_types': _template_preferred_task_types(model),
            'strengths': _template_strengths(model),
            'recommended_roles': recommended_roles,
            'role_scores': role_scores,
            'endpoint_capabilities': {
                'models': True,
                'chat_completions': _endpoint_ok(row, 'chat_completions'),
                'responses': _endpoint_ok(row, 'responses'),
            },
            'probe': {
                'chat_completions': row.get('chat_completions', {}) if isinstance(row, dict) else {},
                'responses': row.get('responses', {}) if isinstance(row, dict) else {},
            },
        })
    summary = {
        'total_models': len(rows),
        'routable_count': sum(1 for row in rows if row['status'] == 'routable'),
        'discovered_count': sum(1 for row in rows if row['status'] == 'discovered'),
        'blocked_count': sum(1 for row in rows if row['status'] == 'blocked'),
        'partial_count': sum(1 for row in rows if row['status'] in {'chat_only', 'responses_only'}),
        'non_chat_count': sum(1 for row in rows if row['status'] == 'non_chat_incompatible'),
        'probe_failed_count': sum(1 for row in rows if row['status'] == 'probe_failed'),
    }
    by_status: dict[str, list[str]] = {}
    for row in rows:
        by_status.setdefault(str(row['status']), []).append(str(row['model_name']))
    return {
        'generated_at': int(time.time()),
        'provider': 'openai',
        'base_url': base_url,
        'default_model': str(default_model or '').strip(),
        'summary': summary,
        'status_index': by_status,
        'models': rows,
    }


def build_orchestrator_templates(
    models: list[str],
    *,
    base_url: str = '',
    validated_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    compatible = sorted({str(model).strip() for model in models if is_text_compatible_model(model)}, key=_family_rank)
    validated_map = _validated_row_map(validated_rows)
    roles = ('code_parallel', 'review_primary', 'plan_primary', 'test_primary', 'docs_primary', 'research_primary')
    runtime_rows: dict[str, dict[str, Any]] = {}
    fallback_statuses = {'routable', 'chat_only', 'responses_only', 'discovered'}
    for model in compatible:
        status = _runtime_status_for_model(model, validated_map.get(model))
        runtime_rows[model] = {
            'runtime_status': status,
            'kernel_eligible': status == 'routable',
            'fallback_candidate': status in fallback_statuses,
        }
    templates_by_role: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        rows: list[dict[str, Any]] = []
        for model in compatible:
            runtime = runtime_rows.get(model, {})
            rows.append({
                'role': role,
                'provider': 'openai',
                'model_name': model,
                'family': _family_name(model),
                'tier': _profile_tier(model),
                'runtime_status': runtime.get('runtime_status', 'discovered'),
                'kernel_eligible': bool(runtime.get('kernel_eligible')),
                'fallback_candidate': bool(runtime.get('fallback_candidate')),
                'preferred_task_types': _template_preferred_task_types(model),
                'strengths': _template_strengths(model),
                'score': _template_score(model, role),
            })
        candidate_rows = [row for row in rows if bool(row.get('fallback_candidate'))]
        active_rows = candidate_rows or rows
        active_rows.sort(
            key=lambda row: (
                _template_availability_rank(str(row.get('runtime_status') or '')),
                -float(row['score']),
                _family_rank(str(row['model_name'])),
            )
        )
        ordered_rows: list[dict[str, Any]] = []
        for rank in sorted({_template_availability_rank(str(row.get('runtime_status') or '')) for row in active_rows}):
            bucket = [row for row in active_rows if _template_availability_rank(str(row.get('runtime_status') or '')) == rank]
            unique_families: set[str] = set()
            diversified: list[dict[str, Any]] = []
            for row in bucket:
                family = str(row.get('family') or '')
                if family and family not in unique_families:
                    diversified.append(row)
                    unique_families.add(family)
                if len(diversified) >= 4:
                    break
            overflow = [row for row in bucket if row not in diversified]
            ordered_rows.extend(diversified + overflow)
        templates_by_role[role] = ordered_rows[:8]

    review_defaults = templates_by_role.get('review_primary') or [{}]
    planning_defaults = templates_by_role.get('plan_primary') or [{}]

    return {
        'generated_at': int(time.time()),
        'provider': 'openai',
        'base_url': base_url,
        'template_count': sum(len(items) for items in templates_by_role.values()),
        'availability_summary': {
            'routable_count': sum(1 for row in runtime_rows.values() if row.get('runtime_status') == 'routable'),
            'partial_count': sum(1 for row in runtime_rows.values() if row.get('runtime_status') in {'chat_only', 'responses_only'}),
            'discovered_count': sum(1 for row in runtime_rows.values() if row.get('runtime_status') == 'discovered'),
            'blocked_count': sum(1 for row in runtime_rows.values() if row.get('runtime_status') == 'blocked'),
            'probe_failed_count': sum(1 for row in runtime_rows.values() if row.get('runtime_status') == 'probe_failed'),
        },
        'roles': templates_by_role,
        'defaults': {
            'code_parallel_branch_count': min(4, len(templates_by_role.get('code_parallel', []))),
            'review_model': review_defaults[0].get('model_name', ''),
            'planning_model': planning_defaults[0].get('model_name', ''),
        },
    }


def sync_openai_compatible_artifacts(
    models: list[str],
    *,
    base_url: str = '',
    validated_rows: list[dict[str, Any]] | None = None,
    default_model: str = '',
) -> dict[str, object]:
    compatible = sorted({str(model).strip() for model in models if is_text_compatible_model(model)}, key=_family_rank)
    openai_family = [model for model in compatible if is_openai_family_model(model)]
    cache_path = Path(os.getenv('OPENAI_MODELS_CACHE_PATH', 'core/.cache/openai_models.json'))
    full_cache_path = Path(os.getenv('OPENAI_MODELS_FULL_CACHE_PATH', 'core/.cache/openai_models_full.json'))
    generated_root = Path(os.getenv('OPENAI_GENERATED_PROFILE_DIR', 'core/mimo/profiles/generated/openai_compatible'))
    templates_path = Path(os.getenv('OPENAI_ORCHESTRATOR_TEMPLATES_PATH', str(generated_root / 'orchestrator_templates.json')))
    model_template_manifest_path = Path(os.getenv('OPENAI_MODEL_TEMPLATE_MANIFEST_PATH', str(generated_root / 'model_template_manifest.json')))
    models_dir = generated_root / 'models'
    combos_dir = generated_root / 'combinations'
    models_dir.mkdir(parents=True, exist_ok=True)
    combos_dir.mkdir(parents=True, exist_ok=True)

    ts = int(time.time())
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({'ts': ts, 'models': compatible}, ensure_ascii=True), encoding='utf-8')

    generated_model_files: list[str] = []
    generated_combo_files: list[str] = []
    seen_model_names: set[str] = set()
    seen_combo_names: set[str] = set()
    for model in compatible:
        payload = build_model_profile(model)
        safe = _sanitize_name(model)
        model_name = f'model__{safe}.json'
        combo_name = f'combo__openai__{safe}.json'
        model_path = models_dir / model_name
        combo_path = combos_dir / combo_name
        model_path.write_text(json.dumps(payload['model'], ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        combo_path.write_text(json.dumps(payload['combo'], ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        if model_name not in seen_model_names:
            generated_model_files.append(str(Path('models') / model_name))
            seen_model_names.add(model_name)
        if combo_name not in seen_combo_names:
            generated_combo_files.append(str(Path('combinations') / combo_name))
            seen_combo_names.add(combo_name)

    template_payload = build_orchestrator_templates(
        compatible,
        base_url=base_url,
        validated_rows=validated_rows,
    )
    templates_path.parent.mkdir(parents=True, exist_ok=True)
    templates_path.write_text(json.dumps(template_payload, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')

    model_template_manifest = build_runtime_model_template_manifest(
        compatible,
        validated_rows=validated_rows,
        base_url=base_url,
        default_model=default_model,
    )
    model_template_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    model_template_manifest_path.write_text(json.dumps(model_template_manifest, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')

    endpoint_manifest = openai_endpoint_manifest(resolve_openai_provider_config())
    generated_manifest = {
        'generated_at': ts,
        'provider': 'openai',
        'base_url': base_url,
        'models': compatible,
        'openai_family_models': openai_family,
        'model_profiles': generated_model_files,
        'combo_profiles': generated_combo_files,
        'orchestrator_templates': str(templates_path.relative_to(generated_root)) if templates_path.is_relative_to(generated_root) else str(templates_path),
        'model_template_manifest': str(model_template_manifest_path.relative_to(generated_root)) if model_template_manifest_path.is_relative_to(generated_root) else str(model_template_manifest_path),
        'endpoint_manifest': endpoint_manifest,
    }
    (generated_root / 'manifest.json').write_text(json.dumps(generated_manifest, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')

    full_payload = {
        'ts': ts,
        'provider': 'openai',
        'base_url': base_url,
        'total_models': len(compatible),
        'openai_family_count': len(openai_family),
        'models': compatible,
        'openai_family_models': openai_family,
        'generated_profile_root': str(generated_root),
        'orchestrator_templates_path': str(templates_path),
        'model_template_manifest_path': str(model_template_manifest_path),
        'endpoint_manifest': endpoint_manifest,
        'model_template_manifest': model_template_manifest,
    }
    full_cache_path.parent.mkdir(parents=True, exist_ok=True)
    full_cache_path.write_text(json.dumps(full_payload, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    return {
        'cache_path': str(cache_path),
        'full_cache_path': str(full_cache_path),
        'generated_profile_root': str(generated_root),
        'orchestrator_templates_path': str(templates_path),
        'model_template_manifest_path': str(model_template_manifest_path),
        'model_template_manifest': model_template_manifest,
        'total_models': len(compatible),
        'openai_family_count': len(openai_family),
    }
