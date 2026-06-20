from __future__ import annotations

from core.core.local_model_manager_module import LocalModelManagerModule
from core.core.local_model_runtime import LocalModelResidentInfo


class _Api:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def log(self, level: str, message: str) -> None:
        self.messages.append((level, message))

    def get_context(self, key: str):
        return None

    def emit_event(self, event_name: str, payload: dict[str, object]) -> None:
        return None

    def query_module_state(self, module_name: str, key: str):
        return None

    def get_memory(self):
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self.config = type('Config', (), {'health_timeout_sec': 1.0})()
        self.resident: dict[str, int] = {}
        self.warmed: list[str] = []
        self.unloaded: list[str] = []

    def list_resident_models_sync(self):
        return [
            LocalModelResidentInfo(name=name, size_vram=size_vram)
            for name, size_vram in sorted(self.resident.items())
        ]

    def warm_model_sync(self, model_name: str, *, keep_alive=None, timeout_sec=None):
        self.warmed.append(model_name)
        self.resident[model_name] = self.resident.get(model_name, 0) or 5 * 1024 ** 3
        return type('WarmResult', (), {'payload': {}, 'metrics': type('Metrics', (), {'load_duration_sec': 0.1})()})()

    def unload_model_sync(self, model_name: str):
        self.unloaded.append(model_name)
        self.resident.pop(model_name, None)
        return True


def test_manager_evicts_idle_local_model_before_warming_new_one(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_LOCAL_MODEL_MEMORY_BUDGET_GB', '10')
    monkeypatch.setenv('AI_BRIDGE_LOCAL_MODEL_PRESSURE_THRESHOLD', '1.0')
    monkeypatch.setenv('AI_BRIDGE_LOCAL_MODEL_MEMORY_MAP', 'qwen-2.5-7b-instruct=5,qwen2.5:32b-instruct-q4_k_m=8')

    runtime = _FakeRuntime()
    runtime.resident['qwen-2.5-7b-instruct'] = 5 * 1024 ** 3
    module = LocalModelManagerModule(runtime=runtime)
    module.on_load(_Api())

    snapshot = module.prepare_for_task('local', 'qwen2.5:32b-instruct-q4_k_m', task_id='task-1')

    unloaded = snapshot['pressure_unloaded'] + snapshot['idle_unloaded']
    assert 'qwen-2.5-7b-instruct' in unloaded
    assert runtime.unloaded == ['qwen-2.5-7b-instruct']
    assert runtime.warmed[-1] == 'qwen2.5:32b-instruct-q4_k_m'


def test_manager_marks_oom_failure_and_unloads_local_model(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_LOCAL_MODEL_MEMORY_MAP', 'qwen2.5:32b-instruct-q4_k_m=8')

    runtime = _FakeRuntime()
    runtime.resident['qwen2.5:32b-instruct-q4_k_m'] = 8 * 1024 ** 3
    module = LocalModelManagerModule(runtime=runtime)
    module.on_load(_Api())

    report = module.handle_failure('local', 'qwen2.5:32b-instruct-q4_k_m', 'HTTP 500: llama-server process has terminated: signal: killed')
    state = module.finalize()

    assert report['oom_detected'] is True
    assert report['unloaded'] is True
    assert runtime.unloaded[-1] == 'qwen2.5:32b-instruct-q4_k_m'
    assert state['blocked_models'][0]['model_name'] == 'qwen2.5:32b-instruct-q4_k_m'


def test_manager_releases_claim_after_task_and_reports_resident_snapshot(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_LOCAL_MODEL_MEMORY_MAP', 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m=9')

    runtime = _FakeRuntime()
    module = LocalModelManagerModule(runtime=runtime)
    module.on_load(_Api())
    module.prepare_for_task('ai_kernel', 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m', task_id='task-2')

    task = type('Task', (), {'task_id': 'task-2'})()
    result = type('Result', (), {'provider': 'ai_kernel', 'model_name': 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m', 'status': type('Status', (), {'value': 'done'})(), 'errors': []})()
    module.after_task(task, result, {'provider': 'ai_kernel', 'model': 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'})
    state = module.finalize()

    assert state['active_tasks'] == {}
    ai_kernel_row = next(item for item in state['models'] if item['provider'] == 'ai_kernel')
    assert ai_kernel_row['active_tasks'] == 0
    assert ai_kernel_row['last_action'] == 'task_claimed'


from core.core.local_model_memory_policy import LocalModelMemoryPolicy


def test_manager_uses_schema_memory_policy_without_env(monkeypatch):
    monkeypatch.delenv('AI_BRIDGE_LOCAL_MODEL_MEMORY_BUDGET_GB', raising=False)
    policy = LocalModelMemoryPolicy(
        total_memory_budget_gb=12.0,
        pressure_threshold=0.75,
        idle_unload_sec=11,
        warm_keep_alive_sec=22,
        oom_cooldown_sec=33,
        model_memory_map={'qwen2.5:32b-instruct-q4_k_m': 7.5},
    )

    module = LocalModelManagerModule(runtime=_FakeRuntime(), policy=policy)
    module.on_load(_Api())
    state = module.finalize()

    assert state['policy']['budget_limit_gb'] == 9.0
    assert state['policy']['idle_unload_sec'] == 11
    assert any(row['estimated_memory_gb'] == 7.5 for row in state['models'] if row['model_name'] == 'qwen2.5:32b-instruct-q4_k_m')
