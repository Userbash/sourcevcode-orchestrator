from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import os
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LearnedModelWeight:
    task_type: str
    model_name: str
    provider: str
    score: float
    samples: int
    effective_samples: float
    success_rate: float
    avg_quality: float
    avg_latency: float
    consistency: float
    source: str


class ExperiencePolicyLearner:
    def __init__(self, weights_path: str | Path | None = None) -> None:
        self.weights_path = Path(weights_path or 'memory_store/experience_policy_weights.json')
        self.min_recommendation_samples = max(3, int(os.getenv('AI_BRIDGE_TRAINING_MIN_SAMPLES', '8') or '8'))
        self.min_trained_memory_quality = max(0.0, min(1.0, float(os.getenv('AI_BRIDGE_TRAINED_MEMORY_MIN_QUALITY', '0.55') or '0.55')))
        self.weights: dict[str, Any] = self._load_weights()

    @staticmethod
    def _infer_provider(model_name: str) -> str:
        normalized = (model_name or '').strip().lower()
        if normalized.startswith('gpt-'):
            return 'openai'
        if normalized.startswith('mistral'):
            return 'mistral'
        if normalized.startswith('claude'):
            return 'anthropic'
        if any(token in normalized for token in ('qwen', 'llama', 'gemma')):
            return 'local'
        return 'local'

    def _load_weights(self) -> dict[str, Any]:
        try:
            if self.weights_path.exists():
                payload = json.loads(self.weights_path.read_text(encoding='utf-8'))
                if isinstance(payload, dict):
                    return payload
        except Exception as exc:
            logger.debug('Experience policy weights load failed: %s', exc)
        return {'updated_at': None, 'task_models': {}, 'task_providers': {}}

    def _save_weights(self) -> None:
        self.weights_path.parent.mkdir(parents=True, exist_ok=True)
        self.weights_path.write_text(json.dumps(self.weights, ensure_ascii=True, indent=2), encoding='utf-8')

    @staticmethod
    def _latency_score(avg_latency: float) -> float:
        if avg_latency <= 0:
            return 0.5
        return max(0.0, 1.0 - min(avg_latency / 5.0, 1.0))

    @staticmethod
    def _smoothed_success_rate(success_total: float, samples: float, *, prior: float = 0.5, strength: float = 2.0) -> float:
        return (success_total + prior * strength) / max(1.0, samples + strength)

    @staticmethod
    def _sample_confidence(samples: float) -> float:
        if samples <= 2.0:
            return 0.0
        return min(1.0, max(0.0, (samples - 2.0) / 8.0))

    @staticmethod
    def _consistency_score(successes: list[bool], quality_scores: list[float]) -> float:
        quality_consistency = 0.6
        success_consistency = 0.6
        if len(quality_scores) >= 2:
            quality_span = max(quality_scores) - min(quality_scores)
            quality_consistency = max(0.0, 1.0 - min(1.0, quality_span / 0.45))
        if len(successes) >= 2:
            success_rate = sum(1.0 for item in successes if item) / len(successes)
            success_consistency = max(0.0, abs(success_rate * 2.0 - 1.0))
        return round((quality_consistency + success_consistency) / 2.0, 4)

    def _aggregate_from_rolling_kpi(self, rolling_kpi_path: Path) -> dict[tuple[str, str], LearnedModelWeight]:
        try:
            payload = json.loads(rolling_kpi_path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}

        learned: dict[tuple[str, str], LearnedModelWeight] = {}
        for key, values in payload.items():
            if not isinstance(values, dict) or '::' not in key:
                continue
            task_type, model_name = key.split('::', 1)
            successes = [bool(item) for item in values.get('successes', [])]
            latencies = [float(item) for item in values.get('latencies', [])]
            quality_scores = [float(item) for item in values.get('quality_scores', [])]
            samples = max(len(successes), len(latencies), len(quality_scores))
            if samples <= 0:
                continue
            success_total = float(sum(successes))
            success_rate = self._smoothed_success_rate(success_total, float(samples))
            avg_latency = sum(latencies) / max(1, len(latencies)) if latencies else 0.0
            avg_quality = sum(quality_scores) / max(1, len(quality_scores)) if quality_scores else success_rate
            consistency = self._consistency_score(successes, quality_scores)
            base_score = success_rate * 0.40 + avg_quality * 0.35 + self._latency_score(avg_latency) * 0.10 + consistency * 0.15
            score = max(0.0, min(1.0, base_score * self._sample_confidence(float(samples))))
            provider = self._infer_provider(model_name)
            learned[(task_type, model_name)] = LearnedModelWeight(
                task_type=task_type,
                model_name=model_name,
                provider=provider,
                score=score,
                samples=samples,
                effective_samples=float(samples),
                success_rate=round(success_rate, 4),
                avg_quality=round(avg_quality, 4),
                avg_latency=round(avg_latency, 4),
                consistency=consistency,
                source='rolling_kpi',
            )
        return learned

    def _merge_trained_memories(self, learned: dict[tuple[str, str], LearnedModelWeight], persistent: Any | None) -> None:
        if persistent is None or not hasattr(persistent, 'list_trained_memories'):
            return
        try:
            records = persistent.list_trained_memories(limit=500)
        except Exception as exc:
            logger.debug('Experience policy could not read trained memories: %s', exc)
            return
        for record in records:
            metadata = dict(getattr(record, 'metadata', {}) or {})
            content = getattr(record, 'content', {})
            if not isinstance(content, dict):
                continue
            task_type = str(content.get('task_type') or metadata.get('task_type') or '').strip().lower()
            model_name = str(metadata.get('model_name') or metadata.get('model') or '').strip()
            provider = str(metadata.get('provider') or '').strip().lower()
            quality_score = float(getattr(record, 'quality_score', 0.0) or 0.0)
            summary = str(content.get('summary') or metadata.get('summary') or '').strip()
            if not task_type or not model_name or quality_score < self.min_trained_memory_quality or len(summary) < 32:
                continue
            if not provider:
                provider = self._infer_provider(model_name)
            key = (task_type, model_name)
            existing = learned.get(key)
            effective_increment = 0.35 if quality_score >= max(self.min_trained_memory_quality, 0.75) else 0.2
            if existing is None:
                success_rate = self._smoothed_success_rate(effective_increment, effective_increment, prior=0.6, strength=3.0)
                consistency = 0.75
                base_score = success_rate * 0.40 + quality_score * 0.35 + self._latency_score(0.0) * 0.10 + consistency * 0.15
                score = max(0.0, min(1.0, base_score * self._sample_confidence(effective_increment)))
                learned[key] = LearnedModelWeight(
                    task_type=task_type,
                    model_name=model_name,
                    provider=provider,
                    score=score,
                    samples=1,
                    effective_samples=effective_increment,
                    success_rate=round(success_rate, 4),
                    avg_quality=round(quality_score, 4),
                    avg_latency=0.0,
                    consistency=consistency,
                    source='trained_memory',
                )
                continue
            total_samples = existing.samples + 1
            total_effective = existing.effective_samples + effective_increment
            existing.avg_quality = round(((existing.avg_quality * existing.effective_samples) + quality_score * effective_increment) / max(total_effective, 1e-9), 4)
            success_total = existing.success_rate * existing.effective_samples + effective_increment
            existing.success_rate = round(self._smoothed_success_rate(success_total, total_effective, prior=0.6, strength=3.0), 4)
            existing.samples = total_samples
            existing.effective_samples = round(total_effective, 4)
            existing.consistency = round(((existing.consistency * (total_effective - effective_increment)) + 0.75 * effective_increment) / max(total_effective, 1e-9), 4)
            blended = existing.success_rate * 0.40 + existing.avg_quality * 0.35 + self._latency_score(existing.avg_latency) * 0.10 + existing.consistency * 0.15
            existing.score = round(max(0.0, min(1.0, blended * self._sample_confidence(total_effective))), 4)
            existing.source = 'rolling_kpi+trained_memory'

    def refresh(self, *, persistent: Any | None = None, rolling_kpi_path: str | Path = 'core/mimo/profiles/rolling_kpi_store.json') -> dict[str, Any]:
        learned = self._aggregate_from_rolling_kpi(Path(rolling_kpi_path))
        self._merge_trained_memories(learned, persistent)

        task_models: dict[str, dict[str, Any]] = {}
        task_providers: dict[str, dict[str, Any]] = {}
        for weight in learned.values():
            task_models.setdefault(weight.task_type, {})[weight.model_name] = {
                'provider': weight.provider,
                'score': round(weight.score, 4),
                'samples': weight.samples,
                'effective_samples': round(weight.effective_samples, 4),
                'success_rate': weight.success_rate,
                'avg_quality': weight.avg_quality,
                'avg_latency': weight.avg_latency,
                'consistency': weight.consistency,
                'source': weight.source,
            }
            provider_bucket = task_providers.setdefault(weight.task_type, {}).setdefault(weight.provider, {'score_total': 0.0, 'samples': 0, 'effective_samples': 0.0})
            provider_bucket['score_total'] += weight.score * weight.effective_samples
            provider_bucket['samples'] += weight.samples
            provider_bucket['effective_samples'] += weight.effective_samples

        normalized_providers: dict[str, dict[str, Any]] = {}
        for task_type, providers in task_providers.items():
            normalized_providers[task_type] = {}
            for provider, payload in providers.items():
                effective_samples = max(0.0001, float(payload['effective_samples']))
                normalized_providers[task_type][provider] = {
                    'score': round(float(payload['score_total']) / effective_samples, 4),
                    'samples': max(1, int(payload['samples'])),
                    'effective_samples': round(effective_samples, 4),
                }

        self.weights = {
            'updated_at': datetime.now(UTC).isoformat(),
            'task_models': task_models,
            'task_providers': normalized_providers,
        }
        self._save_weights()
        return self.weights

    def recommend_model(self, *, task_type: str, allowed_providers: set[str] | None = None, min_samples: int | None = None, min_score: float = 0.65) -> dict[str, Any] | None:
        min_samples = self.min_recommendation_samples if min_samples is None else max(1, int(min_samples))
        min_effective_samples = max(4.0, min_samples * 0.8)
        task_models = self.weights.get('task_models', {}).get(str(task_type).lower(), {})
        if not isinstance(task_models, dict) or not task_models:
            return None
        best_model = None
        best_payload: dict[str, Any] | None = None
        for model_name, payload in task_models.items():
            if not isinstance(payload, dict):
                continue
            provider = str(payload.get('provider') or self._infer_provider(model_name))
            if allowed_providers and provider not in allowed_providers:
                continue
            samples = int(payload.get('samples') or 0)
            effective_samples = float(payload.get('effective_samples') or samples)
            score = float(payload.get('score') or 0.0)
            success_rate = float(payload.get('success_rate') or 0.0)
            avg_quality = float(payload.get('avg_quality') or 0.0)
            consistency = float(payload.get('consistency') or 0.0)
            if samples < min_samples or effective_samples < min_effective_samples or score < min_score:
                continue
            if success_rate < 0.72 or avg_quality < max(self.min_trained_memory_quality, 0.72) or consistency < 0.45:
                continue
            if best_payload is None or score > float(best_payload.get('score') or 0.0):
                best_model = model_name
                best_payload = dict(payload)
                best_payload['provider'] = provider
        if best_model is None or best_payload is None:
            return None
        best_payload['model_name'] = best_model
        return best_payload
