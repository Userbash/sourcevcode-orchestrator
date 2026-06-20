from __future__ import annotations

import asyncio

import httpx

from core.core.local_model_runtime import (
    AsyncLocalModelClient,
    LocalModelClient,
    LocalModelGenerationRequest,
    LocalModelRuntime,
    LocalModelRuntimeConfig,
)


def _response(method: str, url: str, payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    request = httpx.Request(method, url)
    return httpx.Response(status_code, request=request, json=payload)


class _FakeSyncClient:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls: list[tuple[str, str, dict[str, object] | None, float]] = []

    def request(self, method: str, url: str, json=None, timeout=None):
        self.calls.append((method, url, json, timeout))
        handler = self.handlers.pop(0)
        return handler(method, url, json, timeout)

    def close(self) -> None:
        return None


class _FakeAsyncClient:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls: list[tuple[str, str, dict[str, object] | None, float]] = []

    async def request(self, method: str, url: str, json=None, timeout=None):
        self.calls.append((method, url, json, timeout))
        handler = self.handlers.pop(0)
        return handler(method, url, json, timeout)

    async def aclose(self) -> None:
        return None


def test_local_model_client_health_fails_over_between_endpoints():
    config = LocalModelRuntimeConfig(endpoint='http://host.containers.internal:11434', model_name='qwen2.5:32b-instruct-q4_k_m')

    def fail(method, url, json, timeout):
        raise httpx.ConnectError('primary down', request=httpx.Request(method, url))

    def ok(method, url, json, timeout):
        assert url == 'http://127.0.0.1:11434/api/tags'
        return _response(method, url, {'models': [{'name': 'qwen2.5:32b-instruct-q4_k_m'}]})

    client = LocalModelClient(config, client=_FakeSyncClient([fail, fail, ok]))
    health = client.health()

    assert health.ok is True
    assert health.ready is True
    assert health.endpoint == 'http://127.0.0.1:11434'
    assert client.endpoint == 'http://127.0.0.1:11434'


def test_local_model_client_generate_returns_metrics_and_payload():
    config = LocalModelRuntimeConfig(endpoint='http://127.0.0.1:11434', model_name='custom-local')

    def handler(method, url, json, timeout):
        assert json == {
            'model': 'custom-local',
            'prompt': 'ping',
            'system': 'sys',
            'stream': False,
            'options': {'temperature': 0.1},
            'format': {'type': 'json_object'},
        }
        assert timeout == 9.0
        return _response(method, url, {
            'response': '{"ok": true}',
            'done': True,
            'done_reason': 'stop',
            'prompt_eval_count': 11,
            'eval_count': 7,
            'total_duration': 500000000,
            'load_duration': 100000000,
            'prompt_eval_duration': 200000000,
            'eval_duration': 150000000,
        })

    client = LocalModelClient(config, client=_FakeSyncClient([handler]))
    result = client.generate(
        LocalModelGenerationRequest(
            prompt='ping',
            model_name='custom-local',
            system='sys',
            options={'temperature': 0.1},
            timeout_sec=9.0,
            format={'type': 'json_object'},
        )
    )

    assert result.text == '{"ok": true}'
    assert result.payload['done_reason'] == 'stop'
    assert result.metrics.prompt_eval_count == 11
    assert result.metrics.eval_count == 7
    assert result.metrics.endpoint == 'http://127.0.0.1:11434'
    assert result.metrics.finished is True


def test_local_model_runtime_pull_and_unload_sync():
    config = LocalModelRuntimeConfig(endpoint='http://127.0.0.1:11434', model_name='custom-local')
    seen: list[tuple[str, str, dict[str, object] | None]] = []

    def pull(method, url, json, timeout):
        seen.append((method, url, json))
        return _response(method, url, {})

    def health(method, url, json, timeout):
        seen.append((method, url, json))
        return _response(method, url, {'models': [{'name': 'custom-local'}]})

    def unload(method, url, json, timeout):
        seen.append((method, url, json))
        return _response(method, url, {'response': '', 'done': True})

    runtime = LocalModelRuntime(config)
    runtime._sync = LocalModelClient(config, client=_FakeSyncClient([pull, health, unload, health]))

    assert runtime.pull_model_sync('custom-local', timeout_sec=30.0) is True
    assert runtime.unload_model_sync('custom-local') is True
    assert seen[0][2] == {'name': 'custom-local', 'stream': False}
    assert seen[2][2]['keep_alive'] == 0


def test_async_local_model_client_generate_matches_sync_contract():
    config = LocalModelRuntimeConfig(endpoint='http://127.0.0.1:11434', model_name='custom-local')

    def handler(method, url, json, timeout):
        assert json['raw'] is True
        return _response(method, url, {'response': 'pong', 'done': True, 'eval_count': 3})

    async def run() -> None:
        client = AsyncLocalModelClient(config, client=_FakeAsyncClient([handler]))
        result = await client.generate(
            LocalModelGenerationRequest(
                prompt='ping',
                model_name='custom-local',
                raw=True,
            )
        )
        assert result.text == 'pong'
        assert result.metrics.eval_count == 3
        assert result.metrics.endpoint == 'http://127.0.0.1:11434'

    asyncio.run(run())


def test_local_model_client_lists_resident_models():
    config = LocalModelRuntimeConfig(endpoint='http://127.0.0.1:11434', model_name='custom-local')

    def handler(method, url, json, timeout):
        assert method == 'GET'
        assert url == 'http://127.0.0.1:11434/api/ps'
        return _response(method, url, {'models': [{'name': 'custom-local', 'size_vram': 3221225472, 'expires_at': '2026-06-20T12:00:00Z'}]})

    client = LocalModelClient(config, client=_FakeSyncClient([handler]))
    residents = client.list_resident_models()

    assert len(residents) == 1
    assert residents[0].name == 'custom-local'
    assert residents[0].size_vram == 3221225472


def test_local_model_runtime_warm_model_sync_uses_keep_alive():
    config = LocalModelRuntimeConfig(endpoint='http://127.0.0.1:11434', model_name='custom-local')

    def handler(method, url, json, timeout):
        assert json['model'] == 'custom-local'
        assert json['prompt'] == ''
        assert json['keep_alive'] == 120
        assert json['options'] == {'temperature': 0}
        return _response(method, url, {'response': '', 'done': True, 'load_duration': 500000000})

    runtime = LocalModelRuntime(config)
    runtime._sync = LocalModelClient(config, client=_FakeSyncClient([handler]))

    result = runtime.warm_model_sync('custom-local', keep_alive=120, timeout_sec=2.0)

    assert result.model == 'custom-local'
    assert result.metrics.load_duration_sec == 0.5
