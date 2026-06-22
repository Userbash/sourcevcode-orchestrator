from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTAINER_NAME = os.getenv("AI_BRIDGE_LOCAL_LLM_CONTAINER", "ai-kernel-local")
MODEL_NAME = os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m")
OLLAMA_HOST = os.getenv("AI_BRIDGE_LOCAL_LLM_HOST", "0.0.0.0")
OLLAMA_PORT = os.getenv("AI_BRIDGE_LOCAL_LLM_PORT", "11434")


def run_command(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=True, text=True)




def detect_gpu_runtime() -> dict[str, object]:
    forced = os.getenv('AI_BRIDGE_LOCAL_LLM_GPU_BACKEND', 'auto').strip().lower()
    if forced not in {'auto', 'nvidia', 'amd', 'intel', 'cpu'}:
        forced = 'auto'

    def _has_command(cmd: list[str]) -> bool:
        try:
            result = run_command(cmd, check=False)
        except Exception:
            return False
        return result.returncode == 0 and bool((result.stdout or '').strip())

    def _has_path(pattern: str) -> bool:
        return any(Path(item).exists() for item in glob.glob(pattern))

    backend = forced
    if backend == 'auto':
        if _has_command(['nvidia-smi', '-L']):
            backend = 'nvidia'
        elif _has_path('/dev/kfd') or _has_command(['rocminfo']):
            backend = 'amd'
        elif _has_path('/dev/dri/renderD*'):
            backend = 'intel'
        else:
            backend = 'cpu'

    flags: list[str] = [f'--publish {OLLAMA_PORT}:{OLLAMA_PORT}']
    env: dict[str, str] = {'AI_BRIDGE_LOCAL_LLM_GPU_BACKEND_DETECTED': backend}
    if backend == 'nvidia':
        env['OLLAMA_GPU_ENABLED'] = '1'
        env['NVIDIA_VISIBLE_DEVICES'] = 'all'
        env['NVIDIA_DRIVER_CAPABILITIES'] = 'compute,utility'
        flags.extend([
            '--security-opt=label=disable',
            '--group-add keep-groups',
            '--device nvidia.com/gpu=all',
        ])
        return {'backend': backend, 'container_args': [], 'additional_flags': flags, 'env': env}
    if backend == 'amd':
        env['OLLAMA_GPU_ENABLED'] = '1'
        flags.extend(['--device /dev/kfd', '--device /dev/dri', '--group-add keep-groups'])
        return {'backend': backend, 'container_args': [], 'additional_flags': flags, 'env': env}
    if backend == 'intel':
        env['OLLAMA_GPU_ENABLED'] = '1'
        flags.extend(['--device /dev/dri', '--group-add keep-groups'])
        return {'backend': backend, 'container_args': [], 'additional_flags': flags, 'env': env}
    env['OLLAMA_GPU_ENABLED'] = '0'
    return {'backend': 'cpu', 'container_args': [], 'additional_flags': flags, 'env': env}


def gpu_env_exports() -> str:
    runtime = detect_gpu_runtime()
    env = runtime.get('env', {}) if isinstance(runtime.get('env'), dict) else {}
    parts = [f"export {key}={value}" for key, value in env.items() if str(key).strip()]
    return '; '.join(parts)


def distrobox_exists(container_name: str) -> bool:
    result = run_command(["distrobox", "list", "--no-color"], check=False)
    if result.returncode != 0:
        return False
    return container_name in result.stdout


def ensure_container(container_name: str) -> None:
    if distrobox_exists(container_name):
        return
    runtime = detect_gpu_runtime()
    cmd = [
        "distrobox",
        "create",
        "--name",
        container_name,
        "--image",
        "docker.io/library/debian:bookworm",
        "--yes",
    ]
    cmd.extend(str(item) for item in runtime.get('container_args', []) if str(item).strip())
    additional_flags = ' '.join(str(item).strip() for item in runtime.get('additional_flags', []) if str(item).strip())
    if additional_flags:
        cmd.extend(["--additional-flags", additional_flags])
    run_command(cmd)


def install_ollama(container_name: str) -> None:
    install_script = (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update; "
        "apt-get install -y curl ca-certificates python3-pip; "
        "curl -fsSL https://ollama.com/install.sh | sh"
    )
    run_command(["distrobox", "enter", container_name, "--", "bash", "-lc", install_script])


def start_service(container_name: str) -> None:
    gpu_exports = gpu_env_exports()
    serve_cmd = (
        f"set -euo pipefail; "
        + (f"{gpu_exports}; " if gpu_exports else "")
        + f"OLLAMA_HOST={OLLAMA_HOST} OLLAMA_ORIGINS='*' nohup ollama serve > /tmp/ollama.log 2>&1 & "
        + "sleep 5; "
        + f"ollama pull {MODEL_NAME}"
    )
    run_command(["distrobox", "enter", container_name, "--", "bash", "-lc", serve_cmd])


def verify_ready() -> bool:
    probe_url = f"http://127.0.0.1:{OLLAMA_PORT}/api/tags"
    probe = run_command([
        "python3",
        "-c",
        (
            "import json, urllib.request; "
            f"resp = urllib.request.urlopen('{probe_url}', timeout=10); "
            "payload = json.load(resp); "
            "models = payload.get('models', []) if isinstance(payload, dict) else []; "
            f"expected = '{MODEL_NAME}'; "
            "print(expected in {item.get('name', '') for item in models if isinstance(item, dict)})"
        ),
    ], check=False)
    return probe.returncode == 0 and probe.stdout.strip().endswith("True")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision a local Ollama distrobox for AI Bridge")
    parser.add_argument("--container", default=CONTAINER_NAME)
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--port", default=OLLAMA_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    global CONTAINER_NAME, MODEL_NAME, OLLAMA_PORT
    CONTAINER_NAME = args.container
    MODEL_NAME = args.model
    OLLAMA_PORT = args.port

    ensure_container(CONTAINER_NAME)
    install_ollama(CONTAINER_NAME)
    start_service(CONTAINER_NAME)

    if not verify_ready():
        print(f"ERROR: model {MODEL_NAME} is not reachable on {OLLAMA_PORT}.", file=sys.stderr)
        return 1

    print(f"Deployment complete. Ollama bridge is reachable at http://127.0.0.1:{OLLAMA_PORT}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
