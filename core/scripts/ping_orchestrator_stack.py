from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from core.scripts.ping_all_models import PROMPT, resolve_output_dir, run_all_models, write_json


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {'status': 'error', 'error': 'non_object_payload'}


def _provider_ping_summary(report: dict[str, Any], mimo_report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider in ('openai', 'mistral', 'local_llm', 'antigravity', 'ai_kernel'):
        payload = report.get(provider) or {}
        rows.append(
            {
                'provider': provider,
                'ok': int(payload.get('ok') or 0),
                'failed': int(payload.get('failed') or 0),
                'total': len(payload.get('models') or []),
                'error': payload.get('error'),
            }
        )
    rows.append(
        {
            'provider': 'mimo',
            'ok': int(mimo_report.get('ok') or 0),
            'failed': int(mimo_report.get('failed') or 0),
            'total': len(mimo_report.get('models') or []),
            'error': mimo_report.get('error'),
        }
    )
    return rows


def build_markdown_report(
    *,
    health_full: dict[str, Any],
    runtime_inventory: dict[str, Any],
    provider_report: dict[str, Any],
    mimo_report: dict[str, Any],
) -> str:
    summary = health_full.get('summary') or {}
    providers = health_full.get('providers') or []
    agents = health_full.get('agents') or []
    runtime_data = runtime_inventory.get('data') if runtime_inventory.get('status') == 'ok' else {}
    problem_agents = [row for row in agents if row.get('status') != 'ready']
    fully_routable = list((runtime_data or {}).get('fully_routable_models') or [])
    ping_rows = _provider_ping_summary(provider_report, mimo_report)
    timestamp = datetime.now(UTC).isoformat()

    lines = [
        '# Orchestrator Ping-Pong Report',
        '',
        f'- Generated at: `{timestamp}`',
        f'- Ready agents: `{summary.get("ready_agents", 0)}/{summary.get("agent_count", 0)}`',
        f'- Problem agents: `{summary.get("problem_agents", 0)}`',
        f'- Problem providers: `{summary.get("problem_providers", 0)}`',
        f'- OpenAI fully routable models: `{len(fully_routable)}`',
        '',
        '## Agents',
    ]

    if problem_agents:
        for row in problem_agents:
            lines.append(f'- `{row.get("agent_id")}`: `{row.get("status")}`; error=`{row.get("last_error")}`')
    else:
        lines.append('- All registered agents are ready.')

    lines.extend(['', '## Providers'])
    for row in providers:
        lines.append(f'- `{row.get("provider")}`: `{row.get("status")}`; error=`{row.get("error")}`')

    lines.extend(['', '## OpenAI Runtime Inventory'])
    lines.append(f'- Inventory models: `{(runtime_data or {}).get("model_count", 0)}`')
    lines.append(f'- Validated models: `{(runtime_data or {}).get("validated_model_count", 0)}`')
    if fully_routable:
        lines.append(f'- Fully routable: `{", ".join(fully_routable)}`')
    else:
        lines.append('- Fully routable: none')

    lines.extend(['', '## Provider Ping Sweep'])
    for row in ping_rows:
        lines.append(
            f'- `{row["provider"]}`: ok=`{row["ok"]}` failed=`{row["failed"]}` total=`{row["total"]}` error=`{row.get("error")}`'
        )

    priorities: list[str] = []
    provider_map = {str(row.get('provider')): row for row in providers}
    ai_kernel = provider_map.get('ai_kernel') or {}
    mimo = provider_map.get('mimo') or {}
    antigravity = provider_map.get('antigravity') or {}
    openai = provider_map.get('openai') or {}

    if str(ai_kernel.get('status')) not in {'healthy', 'degraded'}:
        priorities.append('P1: поднять реальный AI Kernel service на 8012 или отключить ai_kernel provider до появления backend.')
    if str(mimo.get('status')) != 'healthy':
        priorities.append('P1: исправить MIMO auth; сейчас inventory виден, но usable models отсутствуют из-за auth failures.')
    if str(antigravity.get('status')) != 'healthy':
        priorities.append('P2: активировать Antigravity CLI/API; сейчас auth/CLI path деградирован.')
    if str(openai.get('status')) != 'healthy':
        priorities.append('P2: ограничить routing до fully_routable OpenAI-compatible моделей и не слать задачи в partial/503 модели.')

    lines.extend(['', '## Priorities'])
    if priorities:
        for item in priorities:
            lines.append(f'- {item}')
    else:
        lines.append('- No blocking issues detected.')

    return '\n'.join(lines).strip() + '\n'


async def main_async(args: argparse.Namespace) -> int:
    output_dir = resolve_output_dir(args.output_dir)
    base_url = args.base_url.rstrip('/')
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        health_task = asyncio.create_task(_fetch_json(client, f'{base_url}/health/full'))
        inventory_task = asyncio.create_task(_fetch_json(client, f'{base_url}/providers/openai/runtime_inventory?force_refresh=true&probe_limit=0'))
        ping_task = asyncio.create_task(
            run_all_models(
                args.prompt,
                output_dir,
                skip_mistral_non_chat=not args.include_mistral_non_chat,
                only_provider=args.only_provider,
            )
        )
        health_full, runtime_inventory, ping_tuple = await asyncio.gather(health_task, inventory_task, ping_task)

    provider_report, mimo_report, artifacts = ping_tuple
    markdown = build_markdown_report(
        health_full=health_full,
        runtime_inventory=runtime_inventory,
        provider_report=provider_report,
        mimo_report=mimo_report,
    )

    write_json(output_dir / 'orchestrator_health_full.json', health_full)
    write_json(output_dir / 'openai_runtime_inventory.json', runtime_inventory)
    write_json(output_dir / 'orchestrator_provider_ping_report.json', provider_report)
    write_json(output_dir / 'orchestrator_provider_ping_artifacts.json', artifacts)
    report_path = output_dir / 'orchestrator_stack_report.md'
    report_path.write_text(markdown, encoding='utf-8')

    print(
        json.dumps(
            {
                'status': 'ok',
                'output_dir': str(output_dir),
                'report_path': str(report_path),
                'ready_agents': (health_full.get('summary') or {}).get('ready_agents', 0),
                'problem_agents': (health_full.get('summary') or {}).get('problem_agents', 0),
                'fully_routable_models': ((runtime_inventory.get('data') or {}).get('fully_routable_models') or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run a full orchestrator ping-pong sweep and build a human-readable report.')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000', help='Orchestrator HTTP base URL.')
    parser.add_argument('--prompt', default=PROMPT, help='Prompt to send to model probes.')
    parser.add_argument('--output-dir', default=None, help='Directory for generated reports.')
    parser.add_argument('--include-mistral-non-chat', action='store_true', help='Do not skip non-chat Mistral models.')
    parser.add_argument(
        '--only-provider',
        choices=('openai', 'mistral', 'local_llm', 'mimo', 'antigravity', 'ai_kernel'),
        default=None,
        help='Limit provider model sweeps to one provider.',
    )
    return parser


def main() -> None:
    raise SystemExit(asyncio.run(main_async(build_parser().parse_args())))


if __name__ == '__main__':
    main()
