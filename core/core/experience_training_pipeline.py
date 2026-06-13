from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .experience_policy_learner import ExperiencePolicyLearner

logger = logging.getLogger(__name__)


_PLACEHOLDER_MODEL_RE = re.compile(r"^model[-_:]", re.IGNORECASE)


class ExperienceTrainingPipeline:
    def __init__(
        self,
        *,
        dataset_path: str | Path | None = None,
        adapter_state_path: str | Path | None = None,
        policy_weights_path: str | Path | None = None,
    ) -> None:
        self.dataset_path = Path("memory_store/training/experience_sft_dataset.jsonl") if dataset_path is None else Path(dataset_path)
        self.adapter_state_path = Path("memory_store/training/experience_adapter_state.json") if adapter_state_path is None else Path(adapter_state_path)
        self.policy = ExperiencePolicyLearner(weights_path=policy_weights_path or "memory_store/experience_policy_weights.json")

    @staticmethod
    def _task_family(task_type: str) -> str:
        mapping = {
            "plan": "planning",
            "review": "analysis",
            "research": "analysis",
            "docs": "docs_workflow",
            "test": "verification",
            "code": "implementation",
            "fix": "implementation",
        }
        return mapping.get(task_type, "general")

    @staticmethod
    def _default_temperature(task_type: str) -> float:
        return {
            "plan": 0.75,
            "review": 0.25,
            "research": 0.45,
            "docs": 0.45,
            "test": 0.20,
            "code": 0.20,
            "fix": 0.20,
        }.get(task_type, 0.35)

    @staticmethod
    def _default_context_depth(task_type: str) -> int:
        return {
            "plan": 4,
            "review": 4,
            "research": 3,
            "docs": 2,
            "test": 3,
            "code": 3,
            "fix": 2,
        }.get(task_type, 2)

    @staticmethod
    def _record_summary(record: Any) -> str:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        if isinstance(content, dict):
            for key in ("summary", "result_summary", "brief"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("summary", "result_summary"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _record_task_type(record: Any) -> str:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        raw = ""
        if isinstance(content, dict):
            raw = str(content.get("task_type") or "").strip().lower()
        if not raw:
            raw = str(metadata.get("task_type") or "").strip().lower()
        if not raw:
            raw = str(getattr(record, "memory_domain", "") or "").removeprefix("prompt:").strip().lower()
        return raw or "general"

    @staticmethod
    def _record_model(record: Any) -> tuple[str, str]:
        metadata = getattr(record, "metadata", {}) or {}
        model_name = str(metadata.get("model_name") or metadata.get("model") or "").strip()
        provider = str(metadata.get("provider") or "").strip().lower()
        return model_name, provider

    @staticmethod
    def _normalize_best_practices(summaries: list[str], limit: int = 3) -> list[str]:
        seen: set[str] = set()
        selected: list[str] = []
        for item in summaries:
            text = " ".join(str(item).split())[:240].strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            selected.append(text)
            if len(selected) >= limit:
                break
        return selected

    def _build_dataset_examples(self, records: list[Any]) -> list[dict[str, Any]]:
        examples: list[dict[str, Any]] = []
        for record in records:
            task_type = self._record_task_type(record)
            summary = self._record_summary(record)
            model_name, provider = self._record_model(record)
            quality_score = float(getattr(record, "quality_score", 0.0) or 0.0)
            if not summary:
                continue
            prompt = (
                f"Task type: {task_type}\n"
                f"Memory domain: {getattr(record, 'memory_domain', '')}\n"
                f"Model: {model_name or 'unknown'}\n"
                "Learn a concise successful pattern from the historical execution."
            )
            completion = (
                f"Success pattern: {summary}\n"
                f"Provider: {provider or 'unknown'}\n"
                f"Quality: {quality_score:.2f}"
            )
            examples.append(
                {
                    "task_type": task_type,
                    "memory_domain": getattr(record, "memory_domain", ""),
                    "model_name": model_name,
                    "provider": provider,
                    "quality_score": round(quality_score, 4),
                    "prompt": prompt,
                    "completion": completion,
                    "source_memory_ids": list(getattr(record, "source_memory_ids", []) or []),
                }
            )
        return examples

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

    @staticmethod
    def _usable_model_name(model_name: str) -> bool:
        normalized = (model_name or '').strip()
        if not normalized or normalized.lower() == 'unknown':
            return False
        return _PLACEHOLDER_MODEL_RE.match(normalized) is None

    def _build_kpi_observations(self, persistent: Any | None) -> list[dict[str, Any]]:
        if persistent is None or not hasattr(persistent, 'list_memories'):
            return []
        observations: list[dict[str, Any]] = []
        try:
            rows = persistent.list_memories(limit=1000, memory_type_prefix='kpi_task:')
        except Exception as exc:
            logger.warning('Experience training could not read kpi_task memories: %s', exc)
            return []
        for row in rows:
            content = getattr(row, 'content', {}) or {}
            if not isinstance(content, dict):
                continue
            task_type = str(content.get('task_type') or str(getattr(row, 'memory_type', '')).split(':', 1)[-1]).strip().lower()
            model_name = str(content.get('model') or getattr(row, 'agent_id', '') or '').strip()
            if not self._usable_model_name(model_name):
                continue
            provider = self._infer_provider(model_name)
            observations.append({
                'task_type': task_type or 'general',
                'model_name': model_name,
                'provider': provider,
                'quality_score': float(content.get('quality_score', 0.0) or 0.0),
                'success': bool(content.get('success')),
                'latency': float(content.get('latency', 0.0) or 0.0),
                'budget_pressure': str(content.get('budget_pressure') or ''),
                'profile_weights': dict(content.get('profile_weights') or {}),
                'rolling_kpi': dict(content.get('rolling_kpi') or {}),
                'summary': f"historical_kpi success={bool(content.get('success'))} quality={float(content.get('quality_score', 0.0) or 0.0):.2f}",
            })
        return observations

    def _build_adapter_state(self, records: list[Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for record in records:
            grouped[self._record_task_type(record)].append(record)

        observation_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in observations:
            observation_grouped[str(item.get('task_type') or 'general')].append(item)

        task_profiles: dict[str, dict[str, Any]] = {}
        for task_type in sorted(set(grouped) | set(observation_grouped)):
            items = grouped.get(task_type, [])
            observations = observation_grouped.get(task_type, [])
            summaries_ranked = sorted(items, key=lambda item: float(getattr(item, "quality_score", 0.0) or 0.0), reverse=True)
            model_counts: Counter[str] = Counter()
            provider_counts: Counter[str] = Counter()
            quality_total = 0.0
            samples = 0
            local_candidates: list[tuple[str, float]] = []
            success_total = 0.0
            observed_weights: list[dict[str, float]] = []
            for item in items:
                model_name, provider = self._record_model(item)
                if model_name:
                    model_counts[model_name] += 1
                    if provider == "local":
                        local_candidates.append((model_name, float(getattr(item, "quality_score", 0.0) or 0.0)))
                if provider:
                    provider_counts[provider] += 1
                quality = float(getattr(item, "quality_score", 0.0) or 0.0)
                quality_total += quality
                success_total += 1.0
                samples += 1
            for item in observations:
                model_name = str(item.get('model_name') or '').strip()
                provider = str(item.get('provider') or '').strip().lower()
                quality = float(item.get('quality_score', 0.0) or 0.0)
                success = 1.0 if bool(item.get('success')) else 0.0
                if model_name:
                    model_counts[model_name] += 1
                    if provider == 'local' and (success > 0 or quality >= 0.75):
                        local_candidates.append((model_name, quality + success * 0.1))
                if provider:
                    provider_counts[provider] += 1
                quality_total += quality
                success_total += success
                samples += 1
                weights = item.get('profile_weights')
                if isinstance(weights, dict):
                    observed_weights.append({k: float(v) for k, v in weights.items() if isinstance(v, (int, float))})

            preferred = self.policy.recommend_model(task_type=task_type, allowed_providers={"local"})
            preferred_model = str((preferred or {}).get("model_name") or "").strip()
            preferred_provider = str((preferred or {}).get("provider") or "").strip().lower()
            if not self._usable_model_name(preferred_model):
                preferred_model = ''
                preferred_provider = ''
            if not preferred_model and local_candidates:
                preferred_model = sorted(local_candidates, key=lambda item: item[1], reverse=True)[0][0]
                preferred_provider = 'local'
            avg_quality = round(quality_total / max(1, samples), 4)
            success_rate = round(success_total / max(1, samples), 4)
            best_practices = self._normalize_best_practices([self._record_summary(item) for item in summaries_ranked] + [str(item.get('summary') or '') for item in observations])
            context_depth = self._default_context_depth(task_type)
            if avg_quality >= 0.9:
                context_depth += 1
            profile_weights = {
                'quality': round(1.0 + min(0.5, avg_quality * 0.5), 4),
                'reliability': round(1.0 + min(0.4, samples / 25.0), 4),
                'budget': round(max(0.8, 1.2 - min(0.3, avg_quality * 0.2)), 4),
            }
            if observed_weights:
                for key in ('quality', 'budget', 'vfs'):
                    vals = [weights[key] for weights in observed_weights if key in weights]
                    if vals:
                        profile_weights[key] = round(sum(vals) / len(vals), 4)
            allow_delegate = task_type in {'plan', 'review', 'research', 'docs', 'test'} and avg_quality >= 0.75 and success_rate >= 0.5
            if not preferred_model and success_rate < 0.5:
                preferred_provider = ''
            task_profiles[task_type] = {
                'task_type': task_type,
                'task_family': self._task_family(task_type),
                'samples': samples,
                'avg_quality': avg_quality,
                'success_rate': success_rate,
                'preferred_model': preferred_model,
                'preferred_provider': preferred_provider or ('local' if preferred_model else ''),
                'recommended_model': preferred_model,
                'delegate': allow_delegate,
                'temperature': self._default_temperature(task_type),
                'context_depth': context_depth,
                'profile_weights': profile_weights,
                'best_practices': best_practices,
                'dominant_provider': provider_counts.most_common(1)[0][0] if provider_counts else '',
                'dominant_model': model_counts.most_common(1)[0][0] if model_counts else '',
            }

        return {
            "updated_at": datetime.now(UTC).isoformat(),
            "dataset_path": str(self.dataset_path),
            "policy_weights_path": str(self.policy.weights_path),
            "total_records": len(records),
            "task_profiles": task_profiles,
        }

    def train(self, *, persistent: Any | None = None, rolling_kpi_path: str | Path = "core/mimo/profiles/rolling_kpi_store.json") -> dict[str, Any]:
        records: list[Any] = []
        if persistent is not None and hasattr(persistent, "list_trained_memories"):
            try:
                records = list(persistent.list_trained_memories(limit=1000))
            except Exception as exc:
                logger.warning("Experience training could not read trained memories: %s", exc)
                records = []

        self.policy.refresh(persistent=persistent, rolling_kpi_path=rolling_kpi_path)
        observations = self._build_kpi_observations(persistent)
        dataset_examples = self._build_dataset_examples(records)
        adapter_state = self._build_adapter_state(records, observations)

        self.dataset_path.parent.mkdir(parents=True, exist_ok=True)
        self.adapter_state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.dataset_path.open("w", encoding="utf-8") as handle:
            for row in dataset_examples:
                handle.write(json.dumps(row, ensure_ascii=True) + "\n")
        self.adapter_state_path.write_text(json.dumps(adapter_state, ensure_ascii=True, indent=2), encoding="utf-8")

        return {
            "status": "trained",
            "records": len(records),
            "kpi_observations": len(observations),
            "dataset_examples": len(dataset_examples),
            "dataset_path": str(self.dataset_path),
            "adapter_state_path": str(self.adapter_state_path),
            "task_profiles": len(adapter_state.get("task_profiles", {})),
        }
