from __future__ import annotations

from core.scripts.ping_orchestrator_stack import build_markdown_report


def test_build_markdown_report_contains_key_runtime_sections():
    markdown = build_markdown_report(
        health_full={
            'summary': {'ready_agents': 12, 'agent_count': 13, 'problem_agents': 1, 'problem_providers': 2},
            'providers': [
                {'provider': 'ai_kernel', 'status': 'timeout', 'error': 'tcp_probe_failed'},
                {'provider': 'mimo', 'status': 'degraded', 'error': None},
            ],
            'agents': [
                {'agent_id': 'codex-main', 'status': 'ready', 'last_error': None},
                {'agent_id': 'ai-kernel-qwen36-1', 'status': 'failed', 'last_error': 'connection refused'},
            ],
        },
        runtime_inventory={'status': 'ok', 'data': {'model_count': 24, 'validated_model_count': 26, 'fully_routable_models': ['gpt-5.4', 'gpt-5.5']}},
        provider_report={
            'openai': {'ok': 2, 'failed': 1, 'models': [{}, {}, {}]},
            'mistral': {'ok': 1, 'failed': 0, 'models': [{}]},
            'local_llm': {'ok': 1, 'failed': 0, 'models': [{}]},
            'antigravity': {'ok': 0, 'failed': 1, 'models': [{}], 'error': 'permission_denied'},
            'ai_kernel': {'ok': 0, 'failed': 1, 'models': [], 'error': 'connection refused'},
        },
        mimo_report={'ok': 0, 'failed': 2, 'models': [{}, {}], 'error': 'invalid_api_key'},
    )

    assert '# Orchestrator Ping-Pong Report' in markdown
    assert '`ai-kernel-qwen36-1`' in markdown
    assert 'gpt-5.4, gpt-5.5' in markdown
    assert 'P1: поднять реальный AI Kernel service на 8012' in markdown
