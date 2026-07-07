from core.agents.ai_kernel_agent import AIKernelAgent
from core.core.availability import ModelAvailability, ProviderStatus
from core.core.models import Task, TaskContext, TaskInput, TaskType
from core.core.openai_payload_guard import EMPTY_ASSISTANT_RESPONSE_ERROR, EMPTY_PROVIDER_REQUEST_ERROR
from core.core.provider_inventory_service import ProviderInventoryService
from core.scripts import verify_provider_stack


def _task(description: str) -> Task:
    return Task(
        TaskType.CODE,
        TaskInput(description, files=[]),
        TaskContext("repo", ".", "main"),
    )


def test_ai_kernel_summary_ready(monkeypatch):
    class _Response:
        status_code = 200
        content = b'1'
        def json(self):
            return {'data': [{'id': 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'}]}
    monkeypatch.setattr('core.scripts.verify_provider_stack.requests.get', lambda *args, **kwargs: _Response())
    summary = verify_provider_stack._ai_kernel_summary()
    assert summary['ready'] is True
    assert summary['model_count'] == 1


def test_provider_inventory_service_collects_ai_kernel(monkeypatch):
    monkeypatch.setattr(
        'core.core.ai_kernel_bridge.AIKernelBridge.gate',
        lambda self, model_name=None, ensure_ready=False: {
            'base_url': 'http://127.0.0.1:8012/v1',
            'ready': True,
            'reachable': True,
            'model_alias_present': True,
            'models': ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'],
            'probe': {'ok': True, 'status_code': 200, 'models': ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'], 'error': None},
            'attempted_autostart': ensure_ready,
            'service_process_active': True,
            'autostart_enabled': True,
            'manage_remote_enabled': False,
        },
    )
    service = ProviderInventoryService()
    payload = service.collect(force_refresh=False)
    assert 'ai_kernel' in payload
    assert payload['ai_kernel']['models'] == ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m']


def test_availability_ai_kernel_healthy(monkeypatch):
    class _Socket:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
    class _Response:
        status_code = 200
        content = b'1'
        def json(self):
            return {'data': [{'id': 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'}]}
    monkeypatch.setattr('socket.create_connection', lambda *args, **kwargs: _Socket())
    monkeypatch.setattr('core.core.availability.requests.get', lambda *args, **kwargs: _Response())
    health = ModelAvailability().check_ai_kernel()
    assert health.status == ProviderStatus.HEALTHY


def test_ai_kernel_agent_health_ready(monkeypatch):
    class _Response:
        status_code = 200
        content = b'1'
        def json(self):
            return {'data': [{'id': 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'}]}
    monkeypatch.setattr('core.agents.ai_kernel_agent.httpx.Client.get', lambda *args, **kwargs: _Response())
    health = AIKernelAgent().health()
    assert health.status.value == 'ready'


def test_ai_kernel_agent_health_falls_back_to_host_internal(monkeypatch):
    urls: list[str] = []

    class _Response:
        status_code = 200
        content = b'1'
        def json(self):
            return {'data': [{'id': 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'}]}

    def _fake_get(_self, url, *args, **kwargs):
        urls.append(url)
        if url.startswith('http://127.0.0.1:8012/'):
            raise RuntimeError('loopback refused')
        return _Response()

    monkeypatch.setenv('AI_KERNEL_BASE_URL', 'http://127.0.0.1:8012/v1')
    monkeypatch.setattr('core.agents.ai_kernel_agent.httpx.Client.get', _fake_get)

    health = AIKernelAgent().health()

    assert health.status.value == 'ready'
    assert any('host.containers.internal:8012' in url for url in urls)


def test_ai_kernel_agent_rejects_empty_request_before_client_init(monkeypatch):
    class _UnexpectedOpenAI:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("client should not be created")

    monkeypatch.setattr('core.agents.ai_kernel_agent.OpenAI', _UnexpectedOpenAI)

    result = AIKernelAgent().run(_task(""))

    assert result.status.value == 'failed'
    assert result.errors == [EMPTY_PROVIDER_REQUEST_ERROR]


def test_ai_kernel_agent_rejects_empty_assistant_response(monkeypatch):
    class _ChatCompletions:
        @staticmethod
        def create(*args, **kwargs):
            return {'choices': [{'message': {'content': ''}}]}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            self.chat = type('ChatNamespace', (), {'completions': _ChatCompletions()})()

    monkeypatch.setattr('core.agents.ai_kernel_agent.OpenAI', _Client)

    result = AIKernelAgent().run(_task('Write a patch'))

    assert result.status.value == 'failed'
    assert result.errors == [EMPTY_ASSISTANT_RESPONSE_ERROR]
