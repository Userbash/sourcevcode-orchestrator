from __future__ import annotations

import asyncio

import httpx

from core.scripts import ping_all_models


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, object] | None = None, text: str = '') -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict[str, object]:
        return self._payload


class _FallbackClient:
    async def __aenter__(self) -> '_FallbackClient':
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        if url == 'http://host.containers.internal:11434/api/tags':
            raise httpx.ReadError('inventory failed')
        if url == 'http://127.0.0.1:11434/api/tags':
            return _FakeResponse(payload={'models': [{'name': 'qwen2.5:32b'}]})
        raise AssertionError(f'unexpected url: {url}')

    async def post(self, url: str, headers: dict[str, str] | None = None, json: dict[str, object] | None = None) -> _FakeResponse:
        assert url == 'http://127.0.0.1:11434/api/generate'
        assert (json or {})['model'] == 'qwen2.5:32b'
        return _FakeResponse(payload={'response': 'pong from qwen2.5:32b'})


def test_ping_local_llm_models_falls_back_to_loopback(monkeypatch):
    monkeypatch.delenv('AI_BRIDGE_LOCAL_LLM_ENDPOINT', raising=False)
    monkeypatch.setattr(ping_all_models.httpx, 'AsyncClient', lambda timeout=None: _FallbackClient())

    report = asyncio.run(ping_all_models.ping_local_llm_models('reply with pong only'))

    assert report['ok'] == 1
    assert report['failed'] == 0
    assert report['models'] == [
        {'model': 'qwen2.5:32b', 'status_code': 200, 'ok': True, 'response_sample': 'pong from qwen2.5:32b'}
    ]


def test_run_all_models_captures_provider_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(ping_all_models, 'ping_openai_models', lambda prompt: asyncio.sleep(0, result={'provider': 'openai', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False}))
    monkeypatch.setattr(ping_all_models, 'ping_mistral_models', lambda prompt, skip_non_chat=True: (_ for _ in ()).throw(RuntimeError('mistral boom')))
    monkeypatch.setattr(ping_all_models, 'ping_local_llm_models', lambda prompt: asyncio.sleep(0, result={'provider': 'local_llm', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False}))
    monkeypatch.setattr(ping_all_models, 'ping_mimo_models', lambda prompt, output_dir: asyncio.sleep(0, result={'provider': 'mimo', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False}))
    monkeypatch.setattr(ping_all_models, 'ping_antigravity', lambda prompt: asyncio.sleep(0, result={'provider': 'antigravity', 'models': [], 'ok': 0, 'failed': 0, 'skipped': False}))

    report, mimo_report, artifacts = asyncio.run(ping_all_models.run_all_models('reply with pong only', tmp_path))

    assert report['mistral']['provider'] == 'mistral'
    assert report['mistral']['failed'] == 1
    assert report['mistral']['error'] == 'mistral boom'
    assert mimo_report['provider'] == 'mimo'
    assert artifacts['failed']['mistral']['provider'] == 'mistral'
