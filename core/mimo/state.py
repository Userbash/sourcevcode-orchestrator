from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Deque
import logging

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ModelRuntimeMetrics:
    score: float = 0.5
    successes: int = 0
    failures: int = 0
    last_latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=20))
    recent_statuses: Deque[bool] = field(default_factory=lambda: deque(maxlen=20))


@dataclass(slots=True)
class BudgetLedger:
    balance: float = 0.0
    token_rates: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class ScopedBudget:
    scope: str
    identifier: str
    balance: float = 0.0
    used_tokens: int = 0
    limit_tokens: int = 0

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.limit_tokens - self.used_tokens) if self.limit_tokens > 0 else max(0, int(self.balance))


class MimoStateContext:
    def __init__(self) -> None:
        self._lock = RLock()
        self.financial_pool = BudgetLedger()
        self.model_registry_score: dict[str, ModelRuntimeMetrics] = {}
        self.model_context_limits: dict[str, int] = {}
        self.scoped_budgets: dict[tuple[str, str], ScopedBudget] = {}
        self.default_fallback_model = "gpt-4o"

    def set_rate(self, model_name: str, rate_per_1k_tokens: float) -> None:
        with self._lock:
            self.financial_pool.token_rates[model_name] = max(0.0, rate_per_1k_tokens)

    def set_balance(self, balance: float) -> None:
        with self._lock:
            self.financial_pool.balance = max(0.0, balance)

    def deduct_tokens(self, model_name: str, input_tokens: int, output_tokens: int) -> float:
        with self._lock:
            rate = self.financial_pool.token_rates.get(model_name, 0.0)
            cost = max(0.0, (input_tokens + output_tokens) / 1000.0 * rate)
            self.financial_pool.balance = max(0.0, self.financial_pool.balance - cost)
            logger.info("MIMO budget debit model=%s cost=%.4f balance=%.4f", model_name, cost, self.financial_pool.balance)
            return cost

    def set_context_limit(self, model_name: str, limit_bytes: int) -> None:
        with self._lock:
            self.model_context_limits[model_name] = max(0, limit_bytes)

    def validate_context_limit(self, model_name: str, current_bytes: int) -> bool:
        with self._lock:
            limit = self.model_context_limits.get(model_name)
            return limit is None or current_bytes <= limit

    def update_score(self, model_name: str, is_successful: bool, latency: float) -> float:
        with self._lock:
            metrics = self.model_registry_score.setdefault(model_name, ModelRuntimeMetrics())
            metrics.recent_statuses.append(is_successful)
            metrics.last_latencies.append(max(0.0, latency))
            metrics.successes += int(is_successful)
            metrics.failures += int(not is_successful)
            recent_success_rate = sum(metrics.recent_statuses) / max(1, len(metrics.recent_statuses))
            latency_penalty = min(0.5, sum(metrics.last_latencies) / max(1, len(metrics.last_latencies)) / 1000.0)
            metrics.score = max(0.0, min(1.0, 0.25 + recent_success_rate * 0.7 - latency_penalty))
            logger.debug("MIMO score updated model=%s score=%.3f success=%s latency=%.3f", model_name, metrics.score, is_successful, latency)
            return metrics.score

    def get_allowed_model(self, requested_model: str, task_complexity: str, remaining_budget: float) -> str:
        with self._lock:
            metrics = self.model_registry_score.get(requested_model)
            if remaining_budget <= 0:
                return self.default_fallback_model
            if metrics and metrics.score < 0.4:
                return self.default_fallback_model
            if task_complexity in {"high", "critical"} and remaining_budget < 1.0:
                return self.default_fallback_model
            rate = self.financial_pool.token_rates.get(requested_model, 0.0)
            if rate > 0 and remaining_budget < rate:
                return self.default_fallback_model
            return requested_model

    def set_scoped_budget(self, scope: str, identifier: str, *, balance: float | None = None, limit_tokens: int | None = None) -> None:
        with self._lock:
            key = (scope, identifier)
            budget = self.scoped_budgets.get(key)
            if budget is None:
                budget = ScopedBudget(scope=scope, identifier=identifier)
                self.scoped_budgets[key] = budget
            if balance is not None:
                budget.balance = max(0.0, balance)
            if limit_tokens is not None:
                budget.limit_tokens = max(0, limit_tokens)

    def deduct_scoped_tokens(self, scope: str, identifier: str, tokens: int) -> int:
        with self._lock:
            budget = self.scoped_budgets.setdefault((scope, identifier), ScopedBudget(scope=scope, identifier=identifier))
            budget.used_tokens += max(0, tokens)
            return budget.remaining_tokens

    def get_scoped_budget(self, scope: str, identifier: str) -> ScopedBudget | None:
        with self._lock:
            return self.scoped_budgets.get((scope, identifier))
