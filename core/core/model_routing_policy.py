from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import Complexity, Task, TaskType


MODEL_COST_MULTIPLIERS: dict[str, float] = {
    'gpt-5.5': 4.5,
    'gpt-5.4': 1.0,
    'gpt-5.4-mini': 0.9,
    'gpt-image-2': 177.4,
    'gpt-4o-transcribe': 1.0,
    'claude-opus-4-8': 6.0,
    'claude-opus-4-7': 5.0,
    'claude-opus-4-6': 4.0,
    'claude-opus-4-5': 3.0,
    'claude-sonnet-4-6': 2.5,
    'claude-sonnet-4-5': 1.5,
    'claude-haiku-4-5': 0.75,
    'glm-5.1': 1.55,
    'glm-5': 1.15,
    'qwen3.7-max': 1.1,
    'kimi-k2.6': 1.1,
    'kimi-k2.5': 0.7,
    'mimo-v2-pro': 0.5,
    'qwen3.5-plus': 0.5,
    'mimo-v2-omni': 0.5,
    'hy3-preview': 0.5,
    'minimax-m3': 0.4,
    'qwen3.6-plus': 0.38,
    'minimax-m2.7': 0.38,
    'qwen3.7-plus': 0.3,
    'mimo-v2.5-pro': 0.25,
    'deepseek-v4-pro': 0.25,
    'minimax-m2.5': 0.2,
    'mimo-v2.5': 0.035,
    'deepseek-v4-flash': 0.035,
}

_MODEL_FAMILY_ALIASES: tuple[tuple[str, str], ...] = (
    ('claude-opus', 'claude-opus'),
    ('claude-sonnet', 'claude-sonnet'),
    ('claude-haiku', 'claude-haiku'),
    ('claude', 'claude'),
    ('gpt', 'gpt'),
    ('qwen', 'qwen'),
    ('deepseek', 'deepseek'),
    ('kimi', 'kimi'),
    ('glm', 'glm'),
    ('minimax', 'minimax'),
    ('mimo', 'mimo'),
    ('hy3', 'hy3'),
)

_ERROR_MARKERS_BLOCK_FAMILY = (
    'http error 0',
    'http 429',
    '429 too many requests',
    'claude pool has no eligible resources',
    'claude_pool_error',
)

_FAMILY_RETRY_WINDOW_SEC = 300


@dataclass(slots=True)
class ModelRouteState:
    model_offline_until: dict[str, datetime]
    family_offline_until: dict[str, datetime]
    model_error: dict[str, str]
    family_error: dict[str, str]


class ModelRoutingPolicy:
    _state = ModelRouteState(model_offline_until={}, family_offline_until={}, model_error={}, family_error={})

    @staticmethod
    def _normalize(model_name: str) -> str:
        return str(model_name or '').strip().lower()

    @classmethod
    def family(cls, model_name: str) -> str:
        lowered = cls._normalize(model_name)
        for marker, family in _MODEL_FAMILY_ALIASES:
            if marker in lowered:
                return family
        return 'generic'

    @classmethod
    def multiplier(cls, model_name: str) -> float:
        model = cls._normalize(model_name)
        if not model:
            return 1.0
        return MODEL_COST_MULTIPLIERS.get(model, 1.0)

    @classmethod
    def estimate_cost_usd(cls, tokens: int, model_name: str, *, base_cost_per_million_tokens: float = 1.0) -> float:
        token_count = max(0, int(tokens))
        multiplier = cls.multiplier(model_name)
        return round((token_count / 1_000_000.0) * float(base_cost_per_million_tokens) * multiplier, 6)

    @classmethod
    def _now(cls) -> datetime:
        return datetime.now(UTC)

    @classmethod
    def _is_active_until(cls, until: datetime | None) -> bool:
        return isinstance(until, datetime) and cls._now() < until

    @classmethod
    def is_model_available(cls, model_name: str) -> bool:
        model = cls._normalize(model_name)
        if not model:
            return False
        family = cls.family(model)
        if cls._is_active_until(cls._state.family_offline_until.get(family)):
            return False
        if cls._is_active_until(cls._state.model_offline_until.get(model)):
            return False
        return True

    @classmethod
    def block_model(cls, model_name: str, *, reason: str = '', cooldown_sec: int = _FAMILY_RETRY_WINDOW_SEC, block_family: bool | None = None) -> dict[str, Any]:
        model = cls._normalize(model_name)
        family = cls.family(model)
        reason_l = str(reason or '').strip().lower()
        now = cls._now()
        until = now + timedelta(seconds=max(1, int(cooldown_sec)))
        cls._state.model_offline_until[model] = until
        cls._state.model_error[model] = reason_l
        should_block_family = block_family if block_family is not None else any(marker in reason_l for marker in _ERROR_MARKERS_BLOCK_FAMILY)
        if should_block_family or family in {'claude-opus', 'claude-sonnet', 'claude-haiku'}:
            cls._state.family_offline_until[family] = until
            cls._state.family_error[family] = reason_l
        return {
            'model': model,
            'family': family,
            'offline_until': until.isoformat(),
            'reason': reason_l,
            'blocked_family': should_block_family or family in {'claude-opus', 'claude-sonnet', 'claude-haiku'},
        }

    @classmethod
    def release_model(cls, model_name: str) -> None:
        model = cls._normalize(model_name)
        family = cls.family(model)
        cls._state.model_offline_until.pop(model, None)
        cls._state.model_error.pop(model, None)
        # Release family only when explicitly asked on the family representative.
        if model in {'claude-opus-4-8', 'claude-opus-4-7', 'claude-opus-4-6', 'claude-opus-4-5', 'claude-sonnet-4-6', 'claude-sonnet-4-5', 'claude-haiku-4-5'}:
            cls._state.family_offline_until.pop(family, None)
            cls._state.family_error.pop(family, None)

    @classmethod
    def filter_available(cls, models: list[str]) -> list[str]:
        seen: set[str] = set()
        filtered: list[str] = []
        for model in models:
            normalized = cls._normalize(model)
            if not normalized or normalized in seen or not cls.is_model_available(normalized):
                continue
            seen.add(normalized)
            filtered.append(normalized)
        return filtered

    @staticmethod
    def _task_type(task: Task) -> str:
        return str(getattr(getattr(task, 'type', None), 'value', getattr(task, 'type', 'unknown')) or 'unknown').strip().lower()

    @classmethod
    def _preferred_priority_for_task(cls, task: Task) -> list[str]:
        task_type = cls._task_type(task)
        if task_type == 'code':
            return ['gpt-5.5', 'qwen3.7-max', 'gpt-5.4', 'deepseek-v4-pro', 'gpt-5.4-mini', 'deepseek-v4-flash']
        if task_type == 'review':
            return ['gpt-5.5', 'gpt-5.4', 'claude-opus-4-8', 'qwen3.7-max', 'deepseek-v4-pro', 'gpt-5.4-mini']
        if task_type == 'test':
            return ['gpt-5.4-mini', 'deepseek-v4-flash', 'deepseek-v4-pro', 'gpt-5.4', 'qwen3.7-plus', 'gpt-5.5']
        if task_type == 'docs':
            return ['gpt-5.4-mini', 'qwen3.7-plus', 'deepseek-v4-pro', 'gpt-5.4', 'kimi-k2.5', 'gpt-5.5']
        if task_type == 'fix':
            return ['gpt-5.4', 'deepseek-v4-pro', 'qwen3.7-max', 'gpt-5.4-mini', 'deepseek-v4-flash', 'gpt-5.5']
        if task_type == 'research':
            return ['kimi-k2.6', 'deepseek-v4-pro', 'glm-5.1', 'gpt-5.4', 'qwen3.7-max', 'gpt-5.5']
        return ['gpt-5.4', 'gpt-5.4-mini', 'deepseek-v4-pro', 'qwen3.7-max', 'gpt-5.5']

    @classmethod
    def sort_for_task(cls, task: Task, models: list[str], *, estimated_tokens: int = 0, budget_pressure: str = 'normal') -> list[str]:
        available = cls.filter_available(models)
        if not available:
            return []
        priority = cls._preferred_priority_for_task(task)
        priority_rank = {model: idx for idx, model in enumerate(priority)}
        budget_pressure = str(budget_pressure or 'normal').strip().lower()
        def key(model: str) -> tuple[int, float, str]:
            prio = priority_rank.get(model, len(priority_rank) + 20)
            multiplier = cls.multiplier(model)
            if budget_pressure in {'high', 'critical'}:
                return (prio, multiplier, model)
            if budget_pressure == 'low':
                return (prio, multiplier * 0.9, model)
            return (prio, multiplier, model)
        return sorted(available, key=key)

    @classmethod
    def plan_cost(cls, models: list[str], token_budget: int, *, base_cost_per_million_tokens: float = 1.0) -> dict[str, Any]:
        rows = []
        total = 0.0
        for model in models:
            mult = cls.multiplier(model)
            cost = cls.estimate_cost_usd(token_budget, model, base_cost_per_million_tokens=base_cost_per_million_tokens)
            total += cost
            rows.append({'model': model, 'multiplier': mult, 'estimated_cost_usd': cost})
        return {'estimated_cost_usd': round(total, 6), 'rows': rows, 'base_cost_per_million_tokens': base_cost_per_million_tokens}

    @classmethod
    def family_failover_candidates(cls, model_name: str, task: Task | None = None) -> list[str]:
        family = cls.family(model_name)
        if family == 'claude-opus':
            return ['gpt-5.5', 'qwen3.7-max', 'gpt-5.4', 'deepseek-v4-pro']
        if family == 'claude-sonnet':
            return ['gpt-5.4', 'deepseek-v4-pro', 'qwen3.7-max', 'gpt-5.4-mini']
        if family == 'claude-haiku':
            return ['gpt-5.4-mini', 'deepseek-v4-flash', 'qwen3.7-plus', 'deepseek-v4-pro']
        if family == 'gpt':
            return ['qwen3.7-max', 'deepseek-v4-pro', 'gpt-5.4-mini']
        if family in {'qwen', 'deepseek'}:
            return ['gpt-5.4', 'gpt-5.5', 'deepseek-v4-pro', 'qwen3.7-max']
        return cls._preferred_priority_for_task(task) if task is not None else ['gpt-5.4', 'gpt-5.4-mini', 'deepseek-v4-pro']
