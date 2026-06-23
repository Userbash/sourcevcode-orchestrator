from __future__ import annotations

import os
from pathlib import Path

from .provider_credentials import sync_provider_env_aliases


def _resolve_env_path(path: str) -> Path | None:
    candidate = Path(path)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    search_roots = [
        Path.cwd(),
        Path(__file__).resolve().parents[2],
        Path(__file__).resolve().parents[3],
    ]
    for root in search_roots:
        resolved = (root / path).resolve() if not candidate.is_absolute() else candidate
        if resolved.exists():
            return resolved
    return None


def load_env_file(path: str = ".env", *, override: bool = False) -> None:
    env_path = _resolve_env_path(path)
    if env_path is None:
        sync_provider_env_aliases(os.environ, override=override)
        return

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value

    if Path(path).name == ".env":
        local_secrets_path = env_path.with_name(".env.local.secrets")
        if local_secrets_path.exists():
            for raw in local_secrets_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and (override or key not in os.environ):
                    os.environ[key] = value

    sync_provider_env_aliases(os.environ, override=override)
