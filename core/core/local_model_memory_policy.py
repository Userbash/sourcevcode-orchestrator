from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_MODEL_MEMORY_GB = {
    "qwen2.5:32b-instruct-q4_k_m": 22.0,
    "qwen-2.5-7b-instruct": 6.0,
    "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m": 24.0,
}


@dataclass(slots=True, frozen=True)
class LocalModelMemoryPolicy:
    total_memory_budget_gb: float = 28.0
    pressure_threshold: float = 0.92
    idle_unload_sec: int = 900
    warm_keep_alive_sec: int = 300
    oom_cooldown_sec: int = 600
    model_memory_map: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MODEL_MEMORY_GB))

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> 'LocalModelMemoryPolicy':
        env = environ or os.environ
        return cls(
            total_memory_budget_gb=_env_float(env, 'AI_BRIDGE_LOCAL_MODEL_MEMORY_BUDGET_GB', 28.0),
            pressure_threshold=_env_float(env, 'AI_BRIDGE_LOCAL_MODEL_PRESSURE_THRESHOLD', 0.92),
            idle_unload_sec=_env_int(env, 'AI_BRIDGE_LOCAL_MODEL_IDLE_UNLOAD_SEC', 900),
            warm_keep_alive_sec=_env_int(env, 'AI_BRIDGE_LOCAL_MODEL_WARM_KEEPALIVE_SEC', 300),
            oom_cooldown_sec=_env_int(env, 'AI_BRIDGE_LOCAL_MODEL_OOM_COOLDOWN_SEC', 600),
            model_memory_map=_load_model_memory_map(env),
        )

    def estimated_memory_gb(self, model_name: str) -> float:
        return float(self.model_memory_map.get(model_name, 8.0))

    @property
    def budget_limit_gb(self) -> float:
        return round(self.total_memory_budget_gb * self.pressure_threshold, 3)

    def as_dict(self) -> dict[str, object]:
        return {
            'total_memory_budget_gb': self.total_memory_budget_gb,
            'pressure_threshold': self.pressure_threshold,
            'budget_limit_gb': self.budget_limit_gb,
            'idle_unload_sec': self.idle_unload_sec,
            'warm_keep_alive_sec': self.warm_keep_alive_sec,
            'oom_cooldown_sec': self.oom_cooldown_sec,
            'model_memory_map': dict(self.model_memory_map),
        }


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = str(env.get(name, default)).strip()
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _env_int(env: dict[str, str], name: str, default: int) -> int:
    raw = str(env.get(name, default)).strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _load_model_memory_map(env: dict[str, str]) -> dict[str, float]:
    mapping = dict(DEFAULT_MODEL_MEMORY_GB)
    raw = str(env.get('AI_BRIDGE_LOCAL_MODEL_MEMORY_MAP', '')).strip()
    for item in raw.split(','):
        if '=' not in item:
            continue
        key, value = item.split('=', 1)
        try:
            mapping[key.strip()] = max(0.1, float(value.strip()))
        except ValueError:
            continue
    return mapping
