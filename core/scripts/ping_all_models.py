from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

PROVIDER_CHOICES = ('openai', 'mistral', 'local_llm', 'mimo', 'antigravity', 'ai_kernel')

import httpx

from core.core.env_loader import load_env_file
from core.core.antigravity_provider import extract_antigravity_response_text, invoke_antigravity_native
from core.core.external_ai_bridge import ExternalAIBridge
from core.core.integrations.antigravity_manager import AntigravityManager
from core.core.integrations.mistral_manager import MistralManager
from core.core.openai_compatible_inventory import is_text_compatible_model
from core.core.openai_provider import resolve_openai_provider_config
from core.core.mimo_provider import configured_native_mimo_models, extract_mimo_response_text, invoke_mimo_native

PROMPT = "reply with pong only"
DEFAULT_OUTPUT_DIR = Path("/workspace/reports/model_ping")
MISTRAL_NON_CHAT_MARKERS = (
    "embed",
    "moderation",
    "ocr",
    "tts",
    "transcribe",
    "realtime",
)

load_env_file('.env')
load_env_file('.env.bridge', override=True)
load_env_file('.env.local.secrets', override=True)
load_env_file('.env.gemini.local', override=True)


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def extract_text(payload: Any) -> str:
    if isinstance(payload, dict):
        choices = payload.get("choices") or []
        if choices:
            message = (choices[0] or {}).get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                if parts:
                    return " ".join(parts).strip()
        output = payload.get("output") or []
        parts: list[str] = []
        for item in output:
            for content in (item.get("content") or []):
                if isinstance(content, dict):
                    text = content.get("text") or content.get("output_text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        if parts:
            return " ".join(parts).strip()
        response_text = payload.get("response")
        if isinstance(response_text, str):
            return response_text.strip()
    return ""


def resolve_output_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    if DEFAULT_OUTPUT_DIR.parent.exists():
        return DEFAULT_OUTPUT_DIR
    return Path.cwd() / 'reports' / 'model_ping'


def is_mistral_chat_model(model_id: str) -> bool:
    name = (model_id or '').strip().lower()
    if not name:
        return False
    return not any(marker in name for marker in MISTRAL_NON_CHAT_MARKERS)


def classify_mistral_skip_reason(model_id: str) -> str:
    name = (model_id or '').strip().lower()
    if 'embed' in name:
        return 'embedding_model'
    if 'moderation' in name:
        return 'moderation_model'
    if 'ocr' in name:
        return 'ocr_model'
    if 'tts' in name:
        return 'tts_model'
    if 'transcribe' in name:
        return 'transcription_model'
    if 'realtime' in name:
        return 'realtime_model'
    return 'non_chat_model'


def classify_openai_skip_reason(model_id: str) -> str:
    name = (model_id or '').strip().lower()
    if 'image' in name or 'dall' in name or 'sora' in name:
        return 'image_or_media_model'
    if 'transcribe' in name or 'whisper' in name or 'audio' in name:
        return 'transcription_model'
    if 'tts' in name or 'speech' in name:
        return 'speech_model'
    if 'embedding' in name:
        return 'embedding_model'
    if 'moderation' in name:
        return 'moderation_model'
    if 'realtime' in name:
        return 'realtime_model'
    return 'non_text_model'


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding='utf-8')


def build_artifacts(report: dict[str, Any], mimo_report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    failed: dict[str, Any] = {}
    for name, payload in report.items():
        rows = payload.get('models', [])
        bad = [row for row in rows if not row.get('ok')]
        failed[name] = {
            'provider': name,
            'failed_count': len(bad),
            'total': len(rows),
            'models': bad,
        }
    failed['mimo'] = {
        'provider': 'mimo',
        'failed_count': sum(1 for row in mimo_report.get('models', []) if not row.get('ok')),
        'total': len(mimo_report.get('models', [])),
        'models': [row for row in mimo_report.get('models', []) if not row.get('ok')],
    }
    usable = {
        'provider': 'mimo',
        'usable_count': sum(1 for row in mimo_report.get('models', []) if row.get('ok')),
        'total': len(mimo_report.get('models', [])),
        'models': [row for row in mimo_report.get('models', []) if row.get('ok')],
    }
    return failed, usable


def provider_error_result(provider: str, error: Exception | str) -> dict[str, Any]:
    return {
        'provider': provider,
        'models': [],
        'ok': 0,
        'failed': 1,
        'skipped': False,
        'error': str(error),
    }


def provider_not_selected(provider: str) -> dict[str, Any]:
    return {'provider': provider, 'models': [], 'ok': 0, 'failed': 0, 'skipped': True, 'error': 'not_selected'}


async def ping_ai_kernel_models(prompt: str) -> dict[str, Any]:
    base_url = (os.getenv('AI_KERNEL_BASE_URL') or 'http://127.0.0.1:8012/v1').rstrip('/')
    api_key = os.getenv('AI_KERNEL_API_KEY') or 'local'
    result = {'provider': 'ai_kernel', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        try:
            resp = await client.get(f'{base_url}/models', headers=auth_headers(api_key))
        except Exception as exc:
            return provider_error_result('ai_kernel', exc)
        payload = resp.json() if resp.status_code == 200 else {}
        models = [str(item.get('id') or '').strip() for item in (payload.get('data') or []) if str(item.get('id') or '').strip()] if isinstance(payload, dict) else []
        for model in models:
            row = {'model': model}
            try:
                response = await client.post(f'{base_url}/chat/completions', headers=auth_headers(api_key), json={'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 8})
                row['status_code'] = response.status_code
                if response.status_code < 400:
                    row['ok'] = True
                    row['response_sample'] = extract_text(response.json())[:120]
                    result['ok'] += 1
                else:
                    row['ok'] = False
                    row['error'] = response.text[:240]
                    result['failed'] += 1
            except Exception as exc:
                row['ok'] = False
                row['error'] = str(exc)
                result['failed'] += 1
            result['models'].append(row)
    return result
async def ping_openai_models(prompt: str) -> dict[str, Any]:
    config = resolve_openai_provider_config()
    result = {'provider': 'openai', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False, 'skipped_non_text': 0}
    if not config.api_key:
        result.update({'skipped': True, 'error': 'missing_api_key'})
        return result
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        resp = await client.get(config.models_endpoint, headers=auth_headers(config.api_key))
        payload = resp.json()
        items = payload.get('data') or payload.get('models') or []
        models = []
        for item in items:
            model_id = item.get('id') or item.get('slug') or item.get('name')
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())
        print(f'[openai] discovered {len(models)} models', flush=True)
        runnable: list[str] = []
        rows: list[dict[str, Any]] = []
        for model in models:
            if not is_text_compatible_model(model):
                rows.append({'model': model, 'ok': False, 'skipped': True, 'skip_reason': classify_openai_skip_reason(model)})
                result['skipped_non_text'] += 1
            else:
                runnable.append(model)
        for model in runnable:
            row = {'model': model}
            try:
                response = await client.post(
                    config.chat_completions_endpoint,
                    headers=auth_headers(config.api_key),
                    json={'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 8},
                )
                row['status_code'] = response.status_code
                if response.status_code < 400:
                    row['ok'] = True
                    row['response_sample'] = extract_text(response.json())[:120]
                    result['ok'] += 1
                else:
                    row['ok'] = False
                    row['error'] = response.text[:240]
                    result['failed'] += 1
            except Exception as exc:
                row['ok'] = False
                row['error'] = str(exc)
                result['failed'] += 1
            rows.append(row)
        result['models'] = rows
        print(f"[openai] ok={result['ok']} failed={result['failed']} skipped_non_text={result['skipped_non_text']}", flush=True)
    return result


async def ping_mistral_models(prompt: str, *, skip_non_chat: bool = True) -> dict[str, Any]:
    mgr = MistralManager()
    result = {'provider': 'mistral', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False, 'skipped_non_chat': 0}
    if not mgr.api_key:
        result.update({'skipped': True, 'error': 'missing_api_key'})
        return result
    headers = {'Authorization': f'Bearer {mgr.api_key}', 'Content-Type': 'application/json'}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        response = await client.get(f'{mgr.base_url}/models', headers=headers)
        data = response.json().get('data', []) if response.status_code == 200 else []
        models = [str(item.get('id', '')).strip() for item in data if str(item.get('id', '')).strip()]
        print(f'[mistral] discovered {len(models)} models', flush=True)
        rows: list[dict[str, Any]] = []
        runnable: list[str] = []
        for model in models:
            if skip_non_chat and not is_mistral_chat_model(model):
                rows.append({'model': model, 'ok': False, 'skipped': True, 'skip_reason': classify_mistral_skip_reason(model)})
                result['skipped_non_chat'] += 1
            else:
                runnable.append(model)
        sem = asyncio.Semaphore(8)

        async def one(model: str) -> dict[str, Any]:
            row: dict[str, Any] = {'model': model}
            async with sem:
                try:
                    resp = await client.post(
                        f'{mgr.base_url}/chat/completions',
                        headers=headers,
                        json={'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 8},
                    )
                    row['status_code'] = resp.status_code
                    if resp.status_code < 400:
                        row['ok'] = True
                        row['response_sample'] = extract_text(resp.json())[:120]
                    else:
                        row['ok'] = False
                        row['error'] = resp.text[:240]
                except Exception as exc:
                    row['ok'] = False
                    row['error'] = str(exc)
            return row

        rows.extend(await asyncio.gather(*(one(model) for model in runnable)))
        rows.sort(key=lambda item: item['model'])
        result['models'] = rows
        result['ok'] = sum(1 for row in rows if row.get('ok'))
        result['failed'] = sum(1 for row in rows if not row.get('ok') and not row.get('skipped'))
        print(f"[mistral] ok={result['ok']} failed={result['failed']} skipped_non_chat={result['skipped_non_chat']}", flush=True)
    return result


async def ping_local_llm_models(prompt: str) -> dict[str, Any]:
    base = os.getenv('AI_BRIDGE_LOCAL_LLM_ENDPOINT', 'http://host.containers.internal:11434').rstrip('/')
    result = {'provider': 'local_llm', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False}
    probe_bases = [base]
    if 'host.containers.internal' in base:
        probe_bases.append(base.replace('host.containers.internal', '127.0.0.1'))
    elif '127.0.0.1' in base or 'localhost' in base:
        probe_bases.append(base.replace('127.0.0.1', 'host.containers.internal').replace('localhost', 'host.containers.internal'))
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = None
        last_error = ''
        for candidate in probe_bases:
            try:
                resp = await client.get(f'{candidate}/api/tags')
                base = candidate
                break
            except Exception as exc:
                last_error = str(exc)
        if resp is None:
            result.update({'failed': 1, 'error': last_error or 'local_llm_inventory_unavailable'})
            return result
        models = [str(item.get('name', '')).strip() for item in (resp.json().get('models') or []) if str(item.get('name', '')).strip()]
        print(f'[local_llm] discovered {len(models)} models', flush=True)
        for model in models:
            row = {'model': model}
            try:
                response = await client.post(f'{base}/api/generate', json={'model': model, 'prompt': prompt, 'stream': False})
                row['status_code'] = response.status_code
                if response.status_code < 400:
                    row['ok'] = True
                    row['response_sample'] = str(response.json().get('response') or '').strip()[:120]
                    result['ok'] += 1
                else:
                    row['ok'] = False
                    row['error'] = response.text[:240]
                    result['failed'] += 1
            except Exception as exc:
                row['ok'] = False
                row['error'] = str(exc)
                result['failed'] += 1
            result['models'].append(row)
        print(f"[local_llm] ok={result['ok']} failed={result['failed']}", flush=True)
    return result


async def ping_mimo_models(prompt: str, output_dir: Path) -> dict[str, Any]:
    result = {'provider': 'mimo', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False, 'mode': 'direct_http'}
    models = configured_native_mimo_models()
    if not models:
        result.update({'skipped': True, 'error': 'native_model_catalog_empty'})
        return result
    print(f'[mimo] discovered {len(models)} native models', flush=True)
    partial_path = output_dir / 'mimo_model_ping_report.partial.json'
    sem = asyncio.Semaphore(4)

    def write_partial() -> None:
        payload = {'provider': 'mimo', 'completed': len(result['models']), 'ok': sum(1 for item in result['models'] if item.get('ok')), 'failed': sum(1 for item in result['models'] if not item.get('ok')), 'models': result['models']}
        write_json(partial_path, payload)

    async def one(model: str) -> dict[str, Any]:
        row: dict[str, Any] = {'model': model}
        async with sem:
            payload, error, status_code = await asyncio.to_thread(invoke_mimo_native, model, prompt + ' and no reasoning', timeout_sec=20.0, max_completion_tokens=128, temperature=0.0)
            row['status_code'] = status_code
            text_output = extract_mimo_response_text(payload) if payload else ''
            if text_output:
                row['ok'] = True
                row['response_sample'] = text_output[:120]
            else:
                row['ok'] = False
                row['error'] = str(error or 'no_text_events')[:240]
        return row

    completed = 0
    for coro in asyncio.as_completed([one(model) for model in models]):
        row = await coro
        result['models'].append(row)
        completed += 1
        write_partial()
        if completed % 10 == 0 or completed == len(models):
            ok_count = sum(1 for item in result['models'] if item.get('ok'))
            failed_count = len(result['models']) - ok_count
            print(f'[mimo] progress {completed}/{len(models)} ok={ok_count} failed={failed_count}', flush=True)
    result['models'].sort(key=lambda item: item['model'])
    result['ok'] = sum(1 for item in result['models'] if item.get('ok'))
    result['failed'] = sum(1 for item in result['models'] if not item.get('ok'))
    print(f"[mimo] ok={result['ok']} failed={result['failed']}", flush=True)
    return result


async def ping_antigravity(prompt: str) -> dict[str, Any]:
    manager = AntigravityManager()
    status = manager.status()
    models = list(status.get('models') or [])
    result = {'provider': 'antigravity', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False, 'mode': 'provider_level'}
    if not status.get('ready'):
        error = (status.get('generation_probe') or {}).get('stderr') or (status.get('models_probe') or {}).get('stderr') or 'not_ready'
        result.update({'failed': len(models) or 1, 'error': error})
        for model in models or ['antigravity-pro']:
            result['models'].append({'model': model, 'ok': False, 'error': error})
        return result

    probe_models = models or ['antigravity-pro']

    async def one(model: str) -> dict[str, Any]:
        payload, error, status_code = await asyncio.to_thread(invoke_antigravity_native, model, prompt, timeout_sec=20.0, max_completion_tokens=128, temperature=0.0)
        text_output = extract_antigravity_response_text(payload) if payload else ''
        return {'model': model, 'ok': bool(text_output), 'response_sample': text_output[:120] if text_output else None, 'error': None if text_output else str(error or 'no_text_events')[:240], 'status_code': status_code}

    completed = 0
    for coro in asyncio.as_completed([one(model) for model in probe_models]):
        row = await coro
        result['models'].append(row)
        completed += 1
        if completed % 10 == 0 or completed == len(probe_models):
            ok_count = sum(1 for item in result['models'] if item.get('ok'))
            failed_count = len(result['models']) - ok_count
            print(f'[antigravity] progress {completed}/{len(probe_models)} ok={ok_count} failed={failed_count}', flush=True)
    ordered_rows = {str(item.get('model') or ''): item for item in result['models']}
    result['models'] = [ordered_rows[model] for model in probe_models if model in ordered_rows]
    result['ok'] = sum(1 for item in result['models'] if item.get('ok'))
    result['failed'] = len(result['models']) - result['ok']
    print(f"[antigravity] ok={result['ok']} failed={result['failed']}", flush=True)
    return result


async def run_all_models(prompt: str, output_dir: Path, *, skip_mistral_non_chat: bool = True, only_provider: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    report: dict[str, Any] = {}
    selected = str(only_provider or '').strip() or None

    async def _run_provider(provider: str):
        if selected not in {None, provider}:
            return provider, provider_not_selected(provider)
        try:
            if provider == 'openai':
                return provider, await ping_openai_models(prompt)
            if provider == 'mistral':
                return provider, await ping_mistral_models(prompt, skip_non_chat=skip_mistral_non_chat)
            if provider == 'local_llm':
                return provider, await ping_local_llm_models(prompt)
            if provider == 'mimo':
                return provider, await ping_mimo_models(prompt, output_dir)
            if provider == 'antigravity':
                return provider, await ping_antigravity(prompt)
            if provider == 'ai_kernel':
                return provider, await ping_ai_kernel_models(prompt)
            return provider, provider_error_result(provider, 'unsupported_provider')
        except Exception as exc:
            return provider, provider_error_result(provider, exc)

    results = await asyncio.gather(*(_run_provider(provider) for provider in PROVIDER_CHOICES))
    by_provider = {provider: payload for provider, payload in results}
    mimo_report = by_provider.pop('mimo')
    report.update(by_provider)

    failed, usable = build_artifacts(report, mimo_report)
    return report, mimo_report, {'failed': failed, 'mimo_usable': usable}


async def main_async(args: argparse.Namespace) -> int:
    output_dir = resolve_output_dir(args.output_dir)
    report, mimo_report, artifacts = await run_all_models(args.prompt, output_dir, skip_mistral_non_chat=not args.include_mistral_non_chat, only_provider=args.only_provider)
    write_json(output_dir / 'model_ping_report.json', report)
    write_json(output_dir / 'mimo_model_ping_report.json', mimo_report)
    write_json(output_dir / 'failed_models_by_provider.json', artifacts['failed'])
    write_json(output_dir / 'mimo_usable_models.json', artifacts['mimo_usable'])
    ai_kernel_report = report.get('ai_kernel') or {'ok': 0, 'failed': 0, 'models': []}
    summary = {
        'openai': {'ok': report['openai']['ok'], 'failed': report['openai']['failed'], 'total': len(report['openai']['models'])},
        'mistral': {'ok': report['mistral']['ok'], 'failed': report['mistral']['failed'], 'total': len(report['mistral']['models']), 'skipped_non_chat': report['mistral'].get('skipped_non_chat', 0)},
        'local_llm': {'ok': report['local_llm']['ok'], 'failed': report['local_llm']['failed'], 'total': len(report['local_llm']['models'])},
        'mimo': {'ok': mimo_report['ok'], 'failed': mimo_report['failed'], 'total': len(mimo_report['models'])},
        'antigravity': {'ok': report['antigravity']['ok'], 'failed': report['antigravity']['failed'], 'total': len(report['antigravity']['models'])},
        'ai_kernel': {'ok': ai_kernel_report['ok'], 'failed': ai_kernel_report['failed'], 'total': len(ai_kernel_report['models'])},
    }
    print(json.dumps({'summary': summary, 'output_dir': str(output_dir)}, ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run ping/pong sweeps across provider model inventories and save reports.')
    parser.add_argument('--prompt', default=PROMPT, help='Prompt to send to each model.')
    parser.add_argument('--output-dir', default=None, help='Directory for JSON reports.')
    parser.add_argument('--include-mistral-non-chat', action='store_true', help='Do not skip known non-chat Mistral models.')
    parser.add_argument('--only-provider', choices=PROVIDER_CHOICES, default=None, help='Run the sweep only for one provider, for example mistral.')
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == '__main__':
    main()
