from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.core.ai_kernel_bridge import AIKernelBridge


class _HostBridge:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def execute(self, args, **kwargs):
        self.commands.append(list(args))
        return SimpleNamespace(returncode=0, stdout='', stderr='')


def test_ai_kernel_bridge_starts_service_when_probe_is_down(monkeypatch, tmp_path):
    bridge = AIKernelBridge(
        base_url='http://127.0.0.1:8012/v1',
        model_alias='hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m',
        serve_script=tmp_path / 'serve.sh',
        install_script=tmp_path / 'install.sh',
        log_path=tmp_path / 'ai-kernel.log',
        pid_path=tmp_path / 'ai-kernel.pid',
        startup_timeout_sec=5.0,
        host_bridge=_HostBridge(),
    )
    bridge.serve_script.write_text('''#!/bin/sh
exit 0
''', encoding='utf-8')
    bridge.install_script.write_text('''#!/bin/sh
exit 0
''', encoding='utf-8')

    probes = iter([
        {'ok': False, 'status_code': None, 'models': [], 'error': 'connection refused'},
        {'ok': True, 'status_code': 200, 'models': ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'], 'error': None},
    ])
    monkeypatch.setattr(AIKernelBridge, 'probe', lambda self: next(probes))
    monkeypatch.setattr(AIKernelBridge, '_ensure_dependencies', lambda self: True)
    monkeypatch.setattr(AIKernelBridge, '_service_process_active', lambda self: False)
    monkeypatch.setattr('core.core.ai_kernel_bridge.time.sleep', lambda *_args, **_kwargs: None)

    assert bridge.ensure_ready('hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m') is True
    assert bridge.host_bridge.commands
    assert any('nohup' in ' '.join(cmd) for cmd in bridge.host_bridge.commands)


def test_ai_kernel_bridge_reports_false_when_autostart_disabled(monkeypatch, tmp_path):
    bridge = AIKernelBridge(
        serve_script=tmp_path / 'serve.sh',
        install_script=tmp_path / 'install.sh',
        host_bridge=_HostBridge(),
    )
    monkeypatch.setenv('AI_BRIDGE_AUTOSTART_AI_KERNEL', 'false')
    monkeypatch.setattr(AIKernelBridge, 'probe', lambda self: {'ok': False, 'status_code': None, 'models': [], 'error': 'connection refused'})

    assert bridge.ensure_ready('hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m') is False
    assert bridge.host_bridge.commands == []



def test_ai_kernel_bridge_does_not_manage_remote_runtime_by_default(monkeypatch, tmp_path):
    bridge = AIKernelBridge(
        base_url='http://host.containers.internal:8012/v1',
        serve_script=tmp_path / 'serve.sh',
        install_script=tmp_path / 'install.sh',
        host_bridge=_HostBridge(),
    )
    monkeypatch.setattr(AIKernelBridge, 'probe', lambda self: {'ok': False, 'status_code': None, 'models': [], 'error': 'connection refused'})
    monkeypatch.setenv('AI_BRIDGE_AUTOSTART_AI_KERNEL', 'true')

    assert bridge.ensure_ready('hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m') is False
    assert bridge.host_bridge.commands == []



def test_ai_kernel_bridge_can_manage_remote_runtime_when_enabled(monkeypatch, tmp_path):
    bridge = AIKernelBridge(
        base_url='http://host.containers.internal:8012/v1',
        model_alias='hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m',
        serve_script=tmp_path / 'serve.sh',
        install_script=tmp_path / 'install.sh',
        log_path=tmp_path / 'ai-kernel.log',
        pid_path=tmp_path / 'ai-kernel.pid',
        startup_timeout_sec=5.0,
        host_bridge=_HostBridge(),
    )
    bridge.serve_script.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    bridge.install_script.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')

    probes = iter([
        {'ok': False, 'status_code': None, 'models': [], 'error': 'connection refused'},
        {'ok': True, 'status_code': 200, 'models': ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m'], 'error': None},
    ])
    monkeypatch.setenv('AI_BRIDGE_AUTOSTART_AI_KERNEL', 'true')
    monkeypatch.setenv('AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE', 'true')
    monkeypatch.setattr(AIKernelBridge, 'probe', lambda self: next(probes))
    monkeypatch.setattr(AIKernelBridge, '_ensure_dependencies', lambda self: True)
    monkeypatch.setattr('core.core.ai_kernel_bridge.time.sleep', lambda *_args, **_kwargs: None)

    assert bridge.ensure_ready('hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m') is True
    assert bridge.host_bridge.commands



def test_ai_kernel_bridge_resolves_app_and_root_paths_for_host_runtime(monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_HOST_WORKSPACE_ROOT', '/workspace-host')
    monkeypatch.setenv('AI_KERNEL_HOST_HOME', '/var/home/demo')
    bridge = AIKernelBridge()

    assert bridge._resolve_host_path('scripts/ai-kernel/serve_hauhaucs_qwen36_q4km.sh') == Path('/workspace-host/scripts/ai-kernel/serve_hauhaucs_qwen36_q4km.sh')
    assert bridge._resolve_host_path('/app/scripts/ai-kernel/install_hauhaucs_qwen36.sh') == Path('/workspace-host/scripts/ai-kernel/install_hauhaucs_qwen36.sh')
    assert bridge._resolve_host_path('/root/.cache/ai-kernel/venvs/llama-cpp/bin/python') == Path('/var/home/demo/.cache/ai-kernel/venvs/llama-cpp/bin/python')


def test_ai_kernel_bridge_skips_duplicate_start_when_pid_is_alive(monkeypatch, tmp_path):
    bridge = AIKernelBridge(
        serve_script=tmp_path / 'serve.sh',
        install_script=tmp_path / 'install.sh',
        pid_path=tmp_path / 'ai-kernel.pid',
        host_bridge=_HostBridge(),
    )
    bridge.serve_script.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    bridge.pid_path.write_text('12345\n', encoding='utf-8')
    monkeypatch.setattr(AIKernelBridge, '_service_process_active', lambda self: True)

    assert bridge.start_service() is True
    assert bridge.host_bridge.commands == []
