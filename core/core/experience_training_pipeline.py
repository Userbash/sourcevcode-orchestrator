from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import os

from .experience_policy_learner import ExperiencePolicyLearner
from .training_orchestration import (
    build_experience_training_execution_plan,
    build_experience_training_task_board,
    choose_training_supervisor,
)

logger = logging.getLogger(__name__)


_PLACEHOLDER_MODEL_RE = re.compile(r"^model[-_:]", re.IGNORECASE)
_GENERIC_SUMMARY_PATTERNS = (
    "successful task",
    "completed task",
    "created task graph",
    "done",
    "fixed issue",
)


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
        self.min_summary_chars = max(24, int(os.getenv('AI_BRIDGE_TRAINING_MIN_SUMMARY_CHARS', '48') or '48'))
        self.min_quality = max(0.0, min(1.0, float(os.getenv('AI_BRIDGE_TRAINED_MEMORY_MIN_QUALITY', '0.55') or '0.55')))
        self.min_samples = max(3, int(os.getenv('AI_BRIDGE_TRAINING_MIN_SAMPLES', '5') or '5'))
        self.min_signal_score = max(0.0, min(1.0, float(os.getenv('AI_BRIDGE_TRAINING_MIN_SIGNAL_SCORE', '0.58') or '0.58')))
        self.min_unique_terms = max(4, int(os.getenv('AI_BRIDGE_TRAINING_MIN_UNIQUE_TERMS', '6') or '6'))
        self.min_distinct_patterns = max(2, int(os.getenv('AI_BRIDGE_TRAINING_MIN_DISTINCT_PATTERNS', '3') or '3'))
        self.min_task_success_rate = max(0.5, min(1.0, float(os.getenv('AI_BRIDGE_TRAINING_MIN_SUCCESS_RATE', '0.60') or '0.60')))
        self.min_task_avg_quality = max(self.min_quality, min(1.0, float(os.getenv('AI_BRIDGE_TRAINING_MIN_TASK_QUALITY', '0.70') or '0.70')))
        self.min_model_support = max(2, int(os.getenv('AI_BRIDGE_TRAINING_MIN_MODEL_SUPPORT', '3') or '3'))
        default_effective_samples = max(3.0, self.min_samples * 0.7)
        self.min_effective_samples = max(1.0, float(os.getenv('AI_BRIDGE_TRAINING_MIN_EFFECTIVE_SAMPLES', str(default_effective_samples)) or default_effective_samples))
        self.policy = ExperiencePolicyLearner(weights_path=policy_weights_path or "memory_store/experience_policy_weights.json")
        self.task_board_path = self.dataset_path.parent / "experience_training_task_board.json"

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

    def _record_constraints(self, record: Any) -> list[str]:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        constraints = []
        if isinstance(content, dict):
            constraints = list(content.get("constraints") or [])
        if not constraints:
            constraints = list(metadata.get("constraints") or [])
        return [str(item).strip() for item in constraints if str(item).strip()]

    def _record_files(self, record: Any) -> list[str]:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        files = []
        if isinstance(content, dict):
            files = list(content.get("files") or [])
        if not files:
            files = list(metadata.get("files") or [])
        return [str(item).strip() for item in files if str(item).strip()]

    def _record_problem(self, record: Any) -> str:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        for key in ("problem", "objective", "task_description"):
            if isinstance(content, dict):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _record_failure_mode(self, record: Any) -> str:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        for key in ("failure_mode", "failure_reason"):
            if isinstance(content, dict):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _record_acceptance_criteria(self, record: Any) -> list[str]:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        items = []
        if isinstance(content, dict):
            items = list(content.get("acceptance_criteria") or [])
        if not items:
            items = list(metadata.get("acceptance_criteria") or [])
        return [str(item).strip() for item in items if str(item).strip()]

    def _record_outcome(self, record: Any) -> str:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        if isinstance(content, dict):
            value = content.get("outcome")
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        value = metadata.get("outcome") or metadata.get("status")
        return str(value or "done").strip().lower()

    def _record_reuse_hint(self, record: Any) -> str:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        if isinstance(content, dict):
            value = content.get("reuse_hint")
            if isinstance(value, str) and value.strip():
                return value.strip()
        value = metadata.get("reuse_hint")
        return str(value or "").strip()

    def _record_provenance(self, record: Any) -> str:
        content = getattr(record, "content", {}) or {}
        metadata = getattr(record, "metadata", {}) or {}
        if isinstance(content, dict):
            value = content.get("provenance")
            if isinstance(value, str) and value.strip():
                return value.strip()
        value = metadata.get("source") or metadata.get("provenance")
        return str(value or "").strip()

    @staticmethod
    def _summary_terms(summary: str) -> list[str]:
        normalized = re.sub(r"[^a-z0-9_./:#\-\s]+", " ", str(summary or "").lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return []
        return [term for term in normalized.split(" ") if len(term) >= 3]

    def _summary_is_generic(self, summary: str) -> bool:
        compact = " ".join(str(summary or "").lower().split())
        if any(pattern in compact for pattern in _GENERIC_SUMMARY_PATTERNS):
            return True
        unique_terms = set(self._summary_terms(summary))
        return len(unique_terms) < self.min_unique_terms

    def _record_signal_score(self, record: Any) -> float:
        metadata = getattr(record, "metadata", {}) or {}
        content = getattr(record, "content", {}) or {}
        existing = metadata.get("signal_score")
        if existing is None and isinstance(content, dict):
            existing = content.get("signal_score")
        summary = self._record_summary(record)
        unique_terms = len(set(self._summary_terms(summary)))
        quality_score = float(getattr(record, "quality_score", 0.0) or 0.0)
        problem = self._record_problem(record)
        constraints = self._record_constraints(record)
        files = self._record_files(record)
        acceptance = self._record_acceptance_criteria(record)
        failure_mode = self._record_failure_mode(record)
        reuse_hint = self._record_reuse_hint(record)
        provenance = self._record_provenance(record)
        model_name, provider = self._record_model(record)
        score = 0.0
        score += min(0.26, max(0.0, len(summary) / 240.0) * 0.26)
        score += min(0.18, unique_terms / max(1, self.min_unique_terms * 2)) * 0.18
        score += min(0.22, quality_score * 0.22)
        if problem:
            score += 0.08
        score += min(0.12, (len(constraints) + len(files) + len(acceptance)) * 0.02)
        if failure_mode:
            score += 0.04
        if reuse_hint:
            score += 0.04
        if provenance:
            score += 0.03
        if provider and model_name and model_name.lower() != 'unknown':
            score += 0.03
        if self._summary_is_generic(summary):
            score -= 0.18
        if existing is not None:
            score = max(score, float(existing or 0.0))
        return round(max(0.0, min(1.0, score)), 4)

    def _record_evidence_weight(self, record: Any) -> float:
        quality_score = float(getattr(record, "quality_score", 0.0) or 0.0)
        signal_score = self._record_signal_score(record)
        if not self._record_usable_for_training(record):
            return 0.0
        return round(max(0.15, min(1.0, quality_score * 0.55 + signal_score * 0.45)), 4)

    def _record_usable_for_training(self, record: Any) -> bool:
        summary = self._record_summary(record)
        model_name, _ = self._record_model(record)
        quality_score = float(getattr(record, "quality_score", 0.0) or 0.0)
        if len(summary) < self.min_summary_chars:
            return False
        if quality_score < self.min_quality:
            return False
        if not self._usable_model_name(model_name):
            return False
        if self._summary_is_generic(summary):
            return False
        if self._record_signal_score(record) < self.min_signal_score:
            return False
        return True

    def _build_dataset_examples(self, records: list[Any]) -> list[dict[str, Any]]:
        examples: list[dict[str, Any]] = []
        seen_rows: set[tuple[str, str, str]] = set()
        for record in records:
            if not self._record_usable_for_training(record):
                continue
            task_type = self._record_task_type(record)
            summary = self._record_summary(record)
            model_name, provider = self._record_model(record)
            quality_score = float(getattr(record, "quality_score", 0.0) or 0.0)
            problem = self._record_problem(record)
            constraints = self._record_constraints(record)
            files = self._record_files(record)
            acceptance = self._record_acceptance_criteria(record)
            outcome = self._record_outcome(record)
            failure_mode = self._record_failure_mode(record)
            reuse_hint = self._record_reuse_hint(record)
            provenance = self._record_provenance(record)
            signal_score = self._record_signal_score(record)
            evidence_weight = self._record_evidence_weight(record)
            normalized_summary = " ".join(summary.lower().split())
            row_key = (task_type, model_name.lower(), normalized_summary)
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            task_family = self._task_family(task_type)
            source_memory_ids = list(getattr(record, "source_memory_ids", []) or [])
            prompt = (
                f"Task type: {task_type}\n"
                f"Task family: {task_family}\n"
                f"Memory domain: {getattr(record, 'memory_domain', '')}\n"
                f"Model: {model_name or 'unknown'}\n"
                f"Provider: {provider or 'unknown'}\n"
                f"Problem: {problem or 'n/a'}\n"
                f"Constraints: {', '.join(constraints) if constraints else 'n/a'}\n"
                f"Files: {', '.join(files) if files else 'n/a'}\n"
                f"Acceptance criteria: {', '.join(acceptance) if acceptance else 'n/a'}\n"
                f"Outcome: {outcome}\n"
                f"Failure mode: {failure_mode or 'n/a'}\n"
                "Extract a reusable success pattern and ignore shallow or noisy details."
            )
            completion = (
                f"Success pattern: {summary}\n"
                f"Reuse hint: {reuse_hint or 'Prefer only when constraints/files materially overlap.'}\n"
                f"Failure mode to avoid: {failure_mode or 'none recorded'}\n"
                f"Provenance: {provenance or 'trained_memory'}\n"
                f"Quality: {quality_score:.2f}\n"
                f"Signal: {signal_score:.2f}"
            )
            examples.append(
                {
                    "task_type": task_type,
                    "task_family": task_family,
                    "memory_domain": getattr(record, "memory_domain", ""),
                    "model_name": model_name,
                    "provider": provider,
                    "quality_score": round(quality_score, 4),
                    "signal_score": signal_score,
                    "evidence_weight": evidence_weight,
                    "summary": summary,
                    "summary_length": len(summary),
                    "summary_word_count": len(summary.split()),
                    "problem": problem,
                    "constraints": constraints,
                    "constraint_count": len(constraints),
                    "files": files,
                    "file_count": len(files),
                    "acceptance_criteria": acceptance,
                    "acceptance_criteria_count": len(acceptance),
                    "outcome": outcome,
                    "failure_mode": failure_mode,
                    "reuse_hint": reuse_hint,
                    "provenance": provenance,
                    "source_memory_count": len(source_memory_ids),
                    "prompt": prompt,
                    "completion": completion,
                    "source_memory_ids": source_memory_ids,
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
        if any(token in normalized for token in ('qwen', 'llama', 'gemma')):
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
            quality_score = float(content.get('quality_score', 0.0) or 0.0)
            success = bool(content.get('success'))
            signal_score = round(max(0.15, min(1.0, quality_score * 0.6 + (0.4 if success else 0.15))), 4)
            observations.append({
                'task_type': task_type or 'general',
                'model_name': model_name,
                'provider': provider,
                'quality_score': quality_score,
                'success': success,
                'latency': float(content.get('latency', 0.0) or 0.0),
                'budget_pressure': str(content.get('budget_pressure') or ''),
                'profile_weights': dict(content.get('profile_weights') or {}),
                'rolling_kpi': dict(content.get('rolling_kpi') or {}),
                'signal_score': signal_score,
                'summary': f"historical_kpi success={success} quality={quality_score:.2f}",
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
            usable_items = [item for item in grouped.get(task_type, []) if self._record_usable_for_training(item)]
            observation_items = observation_grouped.get(task_type, [])
            summaries_ranked = sorted(usable_items, key=lambda item: float(getattr(item, 'quality_score', 0.0) or 0.0), reverse=True)
            model_counts: Counter[str] = Counter()
            provider_counts: Counter[str] = Counter()
            model_quality_total: defaultdict[str, float] = defaultdict(float)
            model_success_total: defaultdict[str, float] = defaultdict(float)
            local_support_counts: Counter[str] = Counter()
            quality_total = 0.0
            evidence_total = 0.0
            success_total = 0.0
            local_candidates: list[tuple[str, float]] = []
            observed_weights: list[dict[str, float]] = []
            stable_patterns = {" ".join(self._record_summary(item).lower().split()) for item in usable_items if self._record_summary(item)}
            for item in usable_items:
                model_name, provider = self._record_model(item)
                evidence = self._record_evidence_weight(item)
                quality = float(getattr(item, 'quality_score', 0.0) or 0.0)
                if model_name:
                    model_counts[model_name] += 1
                    model_quality_total[model_name] += quality * evidence
                    model_success_total[model_name] += evidence
                    if provider == 'local':
                        local_support_counts[model_name] += 1
                        local_candidates.append((model_name, quality * evidence))
                if provider:
                    provider_counts[provider] += 1
                quality_total += quality * evidence
                evidence_total += evidence
                success_total += evidence
            for item in observation_items:
                model_name = str(item.get('model_name') or '').strip()
                provider = str(item.get('provider') or '').strip().lower()
                quality = float(item.get('quality_score', 0.0) or 0.0)
                success = 1.0 if bool(item.get('success')) else 0.0
                signal = float(item.get('signal_score', 0.0) or 0.0)
                observation_weight = max(0.1, min(1.0, signal))
                if model_name:
                    model_counts[model_name] += 1
                    model_quality_total[model_name] += quality * observation_weight
                    model_success_total[model_name] += success * observation_weight
                    if provider == 'local' and (success > 0 or quality >= 0.75):
                        local_support_counts[model_name] += 1
                        local_candidates.append((model_name, (quality + success * 0.1) * observation_weight))
                if provider:
                    provider_counts[provider] += 1
                quality_total += quality * observation_weight
                evidence_total += observation_weight
                success_total += success * observation_weight
                weights = item.get('profile_weights')
                if isinstance(weights, dict):
                    observed_weights.append({k: float(v) for k, v in weights.items() if isinstance(v, (int, float))})

            preferred = self.policy.recommend_model(
                task_type=task_type,
                allowed_providers={'local'},
                min_samples=max(self.min_samples, int(math.ceil(self.min_effective_samples))),
            )
            preferred_model = str((preferred or {}).get('model_name') or '').strip()
            preferred_provider = str((preferred or {}).get('provider') or '').strip().lower()
            if not self._usable_model_name(preferred_model):
                preferred_model = ''
                preferred_provider = ''
            avg_quality = round(quality_total / max(1.0, evidence_total), 4)
            success_rate = round(success_total / max(1.0, evidence_total), 4)
            learning_samples = len(usable_items)
            operational_samples = len(observation_items)
            distinct_patterns = len(stable_patterns)
            training_ready = (
                learning_samples >= self.min_samples
                and evidence_total >= self.min_effective_samples
                and distinct_patterns >= self.min_distinct_patterns
                and avg_quality >= self.min_task_avg_quality
                and success_rate >= self.min_task_success_rate
            )
            if not preferred_model and training_ready and local_candidates:
                fallback_model = ''
                fallback_score = -1.0
                for model_name, score in local_candidates:
                    support = local_support_counts[model_name]
                    avg_model_quality = model_quality_total[model_name] / max(1.0, float(support))
                    model_success_rate = model_success_total[model_name] / max(1.0, float(support))
                    if support < self.min_model_support:
                        continue
                    if avg_model_quality < self.min_task_avg_quality or model_success_rate < self.min_task_success_rate:
                        continue
                    if score > fallback_score:
                        fallback_model = model_name
                        fallback_score = score
                if fallback_model:
                    preferred_model = fallback_model
                    preferred_provider = 'local'
            best_practices = self._normalize_best_practices(
                [self._record_summary(item) for item in summaries_ranked]
            )
            context_depth = self._default_context_depth(task_type)
            if avg_quality >= 0.9:
                context_depth += 1
            profile_weights = {
                'quality': round(1.0 + min(0.5, avg_quality * 0.5), 4),
                'reliability': round(1.0 + min(0.5, evidence_total / max(1.0, self.min_effective_samples * 4.0)), 4),
                'budget': round(max(0.8, 1.18 - min(0.25, avg_quality * 0.18)), 4),
            }
            if observed_weights:
                for key in ('quality', 'budget', 'vfs'):
                    vals = [weights[key] for weights in observed_weights if key in weights]
                    if vals:
                        profile_weights[key] = round(sum(vals) / len(vals), 4)
            allow_delegate = (
                training_ready
                and task_type in {'plan', 'review', 'research', 'docs', 'test'}
                and avg_quality >= 0.8
                and success_rate >= 0.75
                and bool(preferred_model)
            )
            stage = 'ready' if training_ready else ('collecting' if learning_samples or operational_samples else 'cold_start')
            task_profiles[task_type] = {
                'task_type': task_type,
                'task_family': self._task_family(task_type),
                'samples': learning_samples + operational_samples,
                'learning_samples': learning_samples,
                'operational_samples': operational_samples,
                'effective_samples': round(evidence_total, 4),
                'distinct_patterns': distinct_patterns,
                'avg_quality': avg_quality,
                'success_rate': success_rate,
                'training_ready': training_ready,
                'training_stage': stage,
                'samples_needed': max(0, self.min_samples - learning_samples),
                'effective_samples_needed': round(max(0.0, self.min_effective_samples - evidence_total), 4),
                'preferred_model': preferred_model if training_ready else '',
                'preferred_provider': (preferred_provider or ('local' if preferred_model else '')) if training_ready else '',
                'recommended_model': preferred_model if training_ready else '',
                'delegate': allow_delegate,
                'temperature': self._default_temperature(task_type),
                'context_depth': context_depth,
                'profile_weights': profile_weights,
                'best_practices': best_practices,
                'dominant_provider': provider_counts.most_common(1)[0][0] if provider_counts else '',
                'dominant_model': model_counts.most_common(1)[0][0] if model_counts else '',
            }

        usable_records = [record for record in records if self._record_usable_for_training(record)]
        return {
            'updated_at': datetime.now(UTC).isoformat(),
            'dataset_path': str(self.dataset_path),
            'policy_weights_path': str(self.policy.weights_path),
            'total_records': len(records),
            'usable_records': len(usable_records),
            'min_samples': self.min_samples,
            'min_effective_samples': self.min_effective_samples,
            'min_signal_score': self.min_signal_score,
            'task_profiles': task_profiles,
        }

    def train(
        self,
        *,
        persistent: Any | None = None,
        rolling_kpi_path: str | Path = 'core/mimo/profiles/rolling_kpi_store.json',
        runtime_snapshot: dict[str, Any] | None = None,
        repo_path: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        records: list[Any] = []
        if persistent is not None and hasattr(persistent, 'list_trained_memories'):
            try:
                records = list(persistent.list_trained_memories(limit=1000))
            except Exception as exc:
                logger.warning('Experience training could not read trained memories: %s', exc)
                records = []

        runtime_snapshot = dict(runtime_snapshot or {})
        self.policy.refresh(persistent=persistent, rolling_kpi_path=rolling_kpi_path)
        observations = self._build_kpi_observations(persistent)
        dataset_examples = self._build_dataset_examples(records)
        adapter_state = self._build_adapter_state(records, observations)
        supervisor = choose_training_supervisor(runtime_snapshot=runtime_snapshot, adapter_state=adapter_state)
        task_board = build_experience_training_task_board(
            adapter_state=adapter_state,
            runtime_snapshot=runtime_snapshot,
            repo_path=repo_path,
            branch=branch,
        )
        execution_plan = build_experience_training_execution_plan(
            adapter_state=adapter_state,
            runtime_snapshot=runtime_snapshot,
            repo_path=repo_path,
            branch=branch,
        )
        adapter_state['training_supervisor'] = supervisor
        adapter_state['training_orchestration'] = {
            'task_board_path': str(self.task_board_path),
            'execution_plan_root_task_id': str(execution_plan.root_task_id),
            'execution_plan_task_count': len(execution_plan.atomic_tasks),
            'merge_order': list(task_board.get('merge_order') or []),
        }
        adapter_state['training_datasets'] = {
            'prompt_learning_examples': len(dataset_examples),
            'operational_kpi_observations': len(observations),
            'separated': True,
        }

        self.dataset_path.parent.mkdir(parents=True, exist_ok=True)
        self.adapter_state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.dataset_path.open('w', encoding='utf-8') as handle:
            for row in dataset_examples:
                handle.write(json.dumps(row, ensure_ascii=True) + "\n")
        self.adapter_state_path.write_text(json.dumps(adapter_state, ensure_ascii=True, indent=2), encoding='utf-8')
        self.task_board_path.write_text(json.dumps(task_board, ensure_ascii=True, indent=2), encoding='utf-8')

        return {
            'status': 'trained',
            'records': len(records),
            'usable_records': len(dataset_examples),
            'kpi_observations': len(observations),
            'dataset_examples': len(dataset_examples),
            'dataset_path': str(self.dataset_path),
            'adapter_state_path': str(self.adapter_state_path),
            'task_profiles': len(adapter_state.get('task_profiles', {})),
            'training_supervisor': supervisor,
            'training_task_board_path': str(self.task_board_path),
        }
