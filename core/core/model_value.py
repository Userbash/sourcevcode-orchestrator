from __future__ import annotations

from typing import Any


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))


def latency_score(latency_ms: float) -> float:
    if latency_ms <= 0:
        return 1.0
    return clamp(1000.0 / (1000.0 + float(latency_ms)))


def cost_efficiency_score(cost_usd: float, *, scale: float = 25.0) -> float:
    return clamp(1.0 / (1.0 + max(0.0, float(cost_usd)) * scale))


def context_fit_score(context_window: int | float | None, *, target_window: int = 200_000) -> float:
    window = float(context_window or 0.0)
    if window <= 0:
        return 0.5
    return clamp(max(0.4, min(1.0, window / float(target_window))))


def memory_efficiency_score(
    *,
    memory_context_bytes: int = 0,
    context_window: int | float | None = None,
    memory_keys_count: int | None = None,
    hot_count: int | None = None,
    hot_capacity: int | None = None,
    persistent_enabled: bool | None = None,
) -> float:
    context_limit = max(1.0, float(context_window or 200_000.0))
    utilization = min(1.0, max(0.0, float(memory_context_bytes) / context_limit))
    key_pressure = 0.0 if memory_keys_count is None else min(1.0, max(0.0, float(memory_keys_count) / 500.0))
    hot_pressure = 0.0
    if hot_count is not None and hot_capacity not in {None, 0}:
        hot_pressure = min(1.0, max(0.0, float(hot_count) / float(hot_capacity)))
    persistence_bonus = 0.06 if persistent_enabled else 0.0
    score = 1.0 - (utilization * 0.55) - (key_pressure * 0.20) - (hot_pressure * 0.19) + persistence_bonus
    return round(clamp(score), 4)


def compute_model_value(
    *,
    success_rate: float,
    quality_score: float,
    latency_ms: float,
    cost_usd: float,
    memory_efficiency: float,
    availability: float = 1.0,
    specialization: float = 1.0,
    context_fit: float = 0.5,
) -> dict[str, Any]:
    success = clamp(success_rate)
    quality = clamp(quality_score)
    latency = latency_score(latency_ms)
    cost = cost_efficiency_score(cost_usd)
    memory = clamp(memory_efficiency)
    availability_score = clamp(availability)
    specialization_score = clamp(specialization)
    context_score = clamp(context_fit)
    value_score = (
        success * 0.24
        + quality * 0.22
        + latency * 0.14
        + cost * 0.12
        + memory * 0.10
        + availability_score * 0.10
        + specialization_score * 0.08
        + context_score * 0.10
    )
    return {
        "value_score": round(clamp(value_score), 6),
        "components": {
            "success_score": round(success, 4),
            "quality_score": round(quality, 4),
            "latency_score": round(latency, 4),
            "cost_efficiency": round(cost, 4),
            "memory_efficiency": round(memory, 4),
            "availability_score": round(availability_score, 4),
            "specialization_score": round(specialization_score, 4),
            "context_fit_score": round(context_score, 4),
        },
    }
