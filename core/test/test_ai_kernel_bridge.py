from __future__ import annotations

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
    monkeypatch.setattr('core.core.ai_kernel_bridge.time.sleep', lambda *_args, **_kwargs: None)

    assert bridge.ensure_ready('hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m') is True
    assert bridge.host_bridge.commands
    assert 'nohup' in ' '.join(bridge.host_bridge.commands[0])


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
