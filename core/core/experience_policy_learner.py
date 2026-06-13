from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LearnedModelWeight:
    task_type: str
    model_name: str
    provider: str
    score: float
    samples: int
    success_rate: float
    avg_quality: float
    avg_latency: float
    source: str


class ExperiencePolicyLearner:
    def __init__(self, weights_path: str | Path | None = None) -> None:
        self.weights_path = Path(weights_path or 'memory_store/experience_policy_weights.json')
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
        if any(token in normalized for token in ('qwen', 'deepseek', 'llama', 'gemma')):
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
            success_rate = sum(successes) / max(1, len(successes))
            avg_latency = sum(latencies) / max(1, len(latencies)) if latencies else 0.0
            avg_quality = sum(quality_scores) / max(1, len(quality_scores)) if quality_scores else success_rate
            score = max(0.0, min(1.0, success_rate * 0.45 + avg_quality * 0.45 + self._latency_score(avg_latency) * 0.10))
            provider = self._infer_provider(model_name)
            learned[(task_type, model_name)] = LearnedModelWeight(
                task_type=task_type,
                model_name=model_name,
                provider=provider,
                score=score,
                samples=samples,
                success_rate=round(success_rate, 4),
                avg_quality=round(avg_quality, 4),
                avg_latency=round(avg_latency, 4),
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
            if not task_type or not model_name:
                continue
            if not provider:
                provider = self._infer_provider(model_name)
            key = (task_type, model_name)
            existing = learned.get(key)
            if existing is None:
                learned[key] = LearnedModelWeight(
                    task_type=task_type,
                    model_name=model_name,
                    provider=provider,
                    score=max(0.0, min(1.0, quality_score)),
                    samples=1,
                    success_rate=1.0,
                    avg_quality=quality_score,
                    avg_latency=0.0,
                    source='trained_memory',
                )
                continue
            total_samples = existing.samples + 1
            existing.avg_quality = round(((existing.avg_quality * existing.samples) + quality_score) / total_samples, 4)
            existing.success_rate = round(min(1.0, ((existing.success_rate * existing.samples) + 1.0) / total_samples), 4)
            existing.samples = total_samples
            existing.score = round(max(0.0, min(1.0, existing.score * 0.8 + quality_score * 0.2)), 4)
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
                'success_rate': weight.success_rate,
                'avg_quality': weight.avg_quality,
                'avg_latency': weight.avg_latency,
                'source': weight.source,
            }
            provider_bucket = task_providers.setdefault(weight.task_type, {}).setdefault(weight.provider, {'score_total': 0.0, 'samples': 0})
            provider_bucket['score_total'] += weight.score * weight.samples
            provider_bucket['samples'] += weight.samples

        normalized_providers: dict[str, dict[str, Any]] = {}
        for task_type, providers in task_providers.items():
            normalized_providers[task_type] = {}
            for provider, payload in providers.items():
                samples = max(1, int(payload['samples']))
                normalized_providers[task_type][provider] = {
                    'score': round(float(payload['score_total']) / samples, 4),
                    'samples': samples,
                }

        self.weights = {
            'updated_at': datetime.now(UTC).isoformat(),
            'task_models': task_models,
            'task_providers': normalized_providers,
        }
        self._save_weights()
        return self.weights

    def recommend_model(self, *, task_type: str, allowed_providers: set[str] | None = None, min_samples: int = 3, min_score: float = 0.65) -> dict[str, Any] | None:
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
            score = float(payload.get('score') or 0.0)
            if samples < min_samples or score < min_score:
                continue
            if best_payload is None or score > float(best_payload.get('score') or 0.0):
                best_model = model_name
                best_payload = dict(payload)
                best_payload['provider'] = provider
        if best_model is None or best_payload is None:
            return None
        best_payload['model_name'] = best_model
        return best_payload
