from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests

from core.core.host_bridge import HostBridge

logger = logging.getLogger("ai_kernel_bridge")


@dataclass(slots=True)
class AIKernelBridge:
    base_url: str = "http://127.0.0.1:8012/v1"
    api_key: str = "local"
    model_alias: str = "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"
    serve_script: Path = Path('scripts/ai-kernel/serve_hauhaucs_qwen36_q4km.sh')
    install_script: Path = Path('scripts/ai-kernel/install_hauhaucs_qwen36.sh')
    log_path: Path = Path('/tmp/ai-kernel-server.log')
    startup_timeout_sec: float = 90.0
    host_bridge: HostBridge = field(default_factory=HostBridge)

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model_alias: str | None = None,
        serve_script: str | Path | None = None,
        install_script: str | Path | None = None,
        log_path: str | Path | None = None,
        startup_timeout_sec: float | None = None,
        host_bridge: HostBridge | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv('AI_KERNEL_BASE_URL') or 'http://127.0.0.1:8012/v1').rstrip('/')
        self.api_key = (api_key or os.getenv('AI_KERNEL_API_KEY') or 'local').strip()
        self.model_alias = (model_alias or os.getenv('AI_KERNEL_MODEL_ALIAS') or 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m').strip()
        self.serve_script = Path(serve_script or os.getenv('AI_KERNEL_SERVE_SCRIPT') or 'scripts/ai-kernel/serve_hauhaucs_qwen36_q4km.sh')
        self.install_script = Path(install_script or os.getenv('AI_KERNEL_INSTALL_SCRIPT') or 'scripts/ai-kernel/install_hauhaucs_qwen36.sh')
        self.log_path = Path(log_path or os.getenv('AI_KERNEL_LOG_PATH') or '/tmp/ai-kernel-server.log')
        try:
            self.startup_timeout_sec = max(5.0, float(startup_timeout_sec if startup_timeout_sec is not None else os.getenv('AI_KERNEL_STARTUP_TIMEOUT_SEC', '90')))
        except ValueError:
            self.startup_timeout_sec = 90.0
        self.host_bridge = host_bridge or HostBridge()

    @staticmethod
    def _autostart_enabled() -> bool:
        return os.getenv('AI_BRIDGE_AUTOSTART_AI_KERNEL', 'true').strip().lower() in {'1', 'true', 'yes', 'on'}

    @staticmethod
    def _auto_install_enabled() -> bool:
        return os.getenv('AI_BRIDGE_AI_KERNEL_AUTO_INSTALL', 'true').strip().lower() in {'1', 'true', 'yes', 'on'}

    @staticmethod
    def _manage_remote_enabled() -> bool:
        return os.getenv('AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}

    def _targets_local_runtime(self) -> bool:
        parsed = urlsplit(self.base_url)
        host = (parsed.hostname or '').strip().lower()
        if host in {'127.0.0.1', 'localhost', '0.0.0.0', ''}:
            return True
        if host == 'host.containers.internal':
            return self._manage_remote_enabled()
        return self._manage_remote_enabled()

    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.api_key}'}

    def _runtime_python(self) -> Path:
        if os.getenv('AI_KERNEL_RUNTIME_PYTHON'):
            return Path(os.getenv('AI_KERNEL_RUNTIME_PYTHON', ''))
        venv_dir = os.getenv('AI_KERNEL_VENV_DIR') or os.path.join(os.path.expanduser(os.getenv('XDG_CACHE_HOME') or '~/.cache'), 'ai-kernel', 'venvs', 'llama-cpp')
        return Path(venv_dir) / 'bin' / 'python'

    def _runtime_dependency_ready(self) -> bool:
        runtime_python = self._runtime_python()
        if not runtime_python.exists():
            return False
        result = self._run([str(runtime_python), '-c', 'import llama_cpp, llama_cpp.server'])
        return result.returncode == 0

    def _run(self, args: list[str], *, check: bool = False, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return self.host_bridge.execute(args, check=check, timeout=timeout)
        except Exception as exc:
            logger.debug('Host bridge execution failed for %s: %s. Falling back to direct execution.', args, exc)
            return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout)

    def probe(self) -> dict[str, object]:
        try:
            response = requests.get(f'{self.base_url}/models', headers=self._headers(), timeout=5.0)
            payload = response.json() if response.content else {}
            models = [str(item.get('id') or '').strip() for item in (payload.get('data') or []) if str(item.get('id') or '').strip()] if isinstance(payload, dict) else []
            return {
                'ok': response.status_code == 200 and bool(models),
                'status_code': response.status_code,
                'models': models,
                'error': None if response.status_code == 200 else response.text[:240],
            }
        except Exception as exc:
            return {'ok': False, 'status_code': None, 'models': [], 'error': str(exc)}

    def _model_ready(self, target_model: str) -> bool:
        probe = self.probe()
        if not probe.get('ok'):
            return False
        models = probe.get('models', [])
        return target_model in models if isinstance(models, list) and target_model else bool(models)

    def _ensure_dependencies(self) -> bool:
        if self.serve_script.exists() and self._runtime_dependency_ready():
            return True
        if not self._auto_install_enabled():
            return False
        if not self.install_script.exists():
            return False
        result = self._run(['bash', str(self.install_script)])
        return result.returncode == 0 and self.serve_script.exists() and self._runtime_dependency_ready()

    def start_service(self) -> bool:
        if not self.serve_script.exists():
            logger.warning('AI kernel serve script is missing: %s', self.serve_script)
            return False
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = (
            f'nohup {shlex.quote(str(self.serve_script))} '
            f'>> {shlex.quote(str(self.log_path))} 2>&1 < /dev/null &'
        )
        result = self._run(['bash', '-lc', cmd])
        return result.returncode == 0

    def ensure_ready(self, model_name: str | None = None) -> bool:
        target_model = (model_name or self.model_alias).strip() or self.model_alias
        if self._model_ready(target_model):
            return True
        if not self._autostart_enabled():
            logger.warning('AI kernel autostart disabled; readiness check failed for %s.', target_model)
            return False
        if not self._targets_local_runtime():
            logger.warning('AI kernel base_url points to external runtime %s; local autostart/install skipped for %s.', self.base_url, target_model)
            return False
        if not self._ensure_dependencies():
            logger.warning('AI kernel dependencies are not ready for %s.', target_model)
            return False
        if not self.start_service():
            logger.warning('AI kernel start_service failed for %s.', target_model)
            return False
        deadline = time.monotonic() + self.startup_timeout_sec
        while time.monotonic() < deadline:
            if self._model_ready(target_model):
                return True
            time.sleep(2)
        logger.warning('AI kernel did not become ready within %.1fs for %s.', self.startup_timeout_sec, target_model)
        return False
