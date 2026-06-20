from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(slots=True)
class ModelFailureState:
    provider: str
    model_name: str
    consecutive_failures: int = 0
    total_failures: int = 0
    last_error_type: str = ""
    last_failure_at: str | None = None
    suppressed_until: str | None = None
    hard_excluded: bool = False


class ModelReplacementPolicy:
    HARD_EXCLUDE_REASONS = {
        'auth_fail',
        'invalid_model',
        'embedding_model',
        'labs_not_enabled',
        'github_pat_not_supported',
        'forbidden',
        'cli_missing_or_unready',
    }
    TRANSIENT_CLOUD_REASONS = {'quota_exhaustion', 'api_timeout', 'tcp_timeout', 'sdk_hang', 'probe_failed'}

    def __init__(self) -> None:
        self.local_soft_failures = self._read_int('AI_BRIDGE_LOCAL_MODEL_SOFT_FAILS', 2)
        self.local_replace_failures = self._read_int('AI_BRIDGE_LOCAL_MODEL_REPLACE_FAILS', 3)
        self.local_hide_failures = self._read_int('AI_BRIDGE_LOCAL_MODEL_HIDE_FAILS', 5)
        self.cloud_soft_failures = self._read_int('AI_BRIDGE_CLOUD_MODEL_SOFT_FAILS', 2)
        self.cloud_replace_failures = self._read_int('AI_BRIDGE_CLOUD_MODEL_REPLACE_FAILS', 2)
        self.cloud_cooldown_sec = self._read_int('AI_BRIDGE_CLOUD_MODEL_COOLDOWN_SEC', 900)
        self._states: dict[tuple[str, str], ModelFailureState] = {}

    @staticmethod
    def _read_int(key: str, default: int) -> int:
        raw = os.getenv(key, str(default)).strip()
        try:
            return int(raw)
        except ValueError:
            return default

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        raw = str(provider or '').strip().lower()
        if raw in {'google', 'gemini', 'antigravity', 'agy', 'antigravity-cli'}:
            return 'antigravity'
        if raw in {'local_llm', 'ollama'}:
            return 'local'
        return raw

    @classmethod
    def _provider_kind(cls, provider: str) -> str:
        return 'local' if cls._normalize_provider(provider) == 'local' else 'cloud'

    def _state(self, provider: str, model_name: str) -> ModelFailureState:
        key = (self._normalize_provider(provider), str(model_name or '').strip())
        state = self._states.get(key)
        if state is None:
            state = ModelFailureState(provider=key[0], model_name=key[1])
            self._states[key] = state
        return state

    @staticmethod
    def _parse_ts(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    def register_failure(self, provider: str, model_name: str, reason: str) -> dict[str, Any]:
        state = self._state(provider, model_name)
        reason = str(reason or 'probe_failed').strip().lower()
        now = datetime.now(UTC)
        state.total_failures += 1
        state.consecutive_failures += 1
        state.last_error_type = reason
        state.last_failure_at = now.isoformat()

        if self._provider_kind(provider) == 'local':
            if state.consecutive_failures >= self.local_hide_failures:
                state.hard_excluded = True
            elif state.consecutive_failures >= self.local_replace_failures:
                state.suppressed_until = (now + timedelta(hours=1)).isoformat()
        else:
            if reason in self.HARD_EXCLUDE_REASONS:
                state.hard_excluded = True
                state.suppressed_until = None
            elif reason in self.TRANSIENT_CLOUD_REASONS and state.consecutive_failures >= self.cloud_replace_failures:
                state.suppressed_until = (now + timedelta(seconds=self.cloud_cooldown_sec)).isoformat()
        return self.decision(provider, model_name)

    def register_success(self, provider: str, model_name: str) -> None:
        state = self._state(provider, model_name)
        state.consecutive_failures = 0
        state.last_error_type = ''
        state.last_failure_at = None
        state.suppressed_until = None
        state.hard_excluded = False

    def decision(self, provider: str, model_name: str) -> dict[str, Any]:
        state = self._state(provider, model_name)
        suppressed_until = self._parse_ts(state.suppressed_until)
        now = datetime.now(UTC)
        cooldown_active = bool(suppressed_until and suppressed_until > now)
        kind = self._provider_kind(provider)
        replacement_due = False
        hide_from_catalog = False
        if kind == 'local':
            replacement_due = state.consecutive_failures >= self.local_replace_failures or state.hard_excluded
            hide_from_catalog = state.consecutive_failures >= self.local_hide_failures or state.hard_excluded
        else:
            replacement_due = state.hard_excluded or cooldown_active or state.consecutive_failures >= self.cloud_replace_failures
            hide_from_catalog = state.hard_excluded
        return {
            'provider': state.provider,
            'model_name': state.model_name,
            'kind': kind,
            'consecutive_failures': state.consecutive_failures,
            'total_failures': state.total_failures,
            'last_error_type': state.last_error_type,
            'last_failure_at': state.last_failure_at,
            'suppressed_until': state.suppressed_until,
            'hard_excluded': state.hard_excluded,
            'cooldown_active': cooldown_active,
            'soft_degraded': state.consecutive_failures >= (self.local_soft_failures if kind == 'local' else self.cloud_soft_failures),
            'replacement_due': replacement_due,
            'hide_from_catalog': hide_from_catalog,
        }

    def failure_snapshot(self) -> dict[str, Any]:
        entries = [self.decision(provider, model_name) for provider, model_name in sorted(self._states)]
        return {
            'thresholds': {
                'local_soft_failures': self.local_soft_failures,
                'local_replace_failures': self.local_replace_failures,
                'local_hide_failures': self.local_hide_failures,
                'cloud_soft_failures': self.cloud_soft_failures,
                'cloud_replace_failures': self.cloud_replace_failures,
                'cloud_cooldown_sec': self.cloud_cooldown_sec,
            },
            'models': entries,
        }

    @staticmethod
    def _task_type(task: Any) -> str:
        value = getattr(getattr(task, 'type', None), 'value', getattr(task, 'type', 'unknown'))
        return str(value or 'unknown').strip().lower()

    @staticmethod
    def _complexity(task: Any) -> str:
        value = getattr(getattr(task, 'complexity', None), 'value', getattr(task, 'complexity', 'medium'))
        return str(value or 'medium').strip().lower()

    @staticmethod
    def _priority(task: Any) -> str:
        value = getattr(getattr(task, 'priority', None), 'value', getattr(task, 'priority', 'normal'))
        return str(value or 'normal').strip().lower()

    @staticmethod
    def _task_text(task: Any) -> str:
        description = str(getattr(getattr(task, 'input', None), 'description', '') or '')
        constraints = getattr(getattr(task, 'input', None), 'constraints', []) or []
        return f"{description} {' '.join(str(item) for item in constraints)}".lower()

    @classmethod
    def _high_risk(cls, task: Any) -> bool:
        text = cls._task_text(task)
        priority = cls._priority(task)
        markers = {'security', 'auth', 'rbac', 'payment', 'production', 'migration', 'destructive'}
        return priority == 'critical' or any(token in text for token in markers)

    @staticmethod
    def _push_unique(rows: list[dict[str, str]], seen: set[tuple[str, str]], provider: str, model_name: str) -> None:
        key = (provider, model_name)
        if not provider or not model_name or key in seen:
            return
        seen.add(key)
        rows.append({'provider': provider, 'model_name': model_name})

    def _candidate_pairs(self, provider: str, model_name: str, task: Any | None = None, failure_reason: str | None = None) -> list[dict[str, str]]:
        provider = self._normalize_provider(provider)
        model_name = str(model_name or '').strip()
        task_type = self._task_type(task) if task is not None else 'code'
        high_risk = self._high_risk(task) if task is not None else False
        reason = str(failure_reason or '').strip().lower()
        seen: set[tuple[str, str]] = set()
        rows: list[dict[str, str]] = []

        if provider == 'local':
            if 'qwen' in model_name:
                self._push_unique(rows, seen, 'ai_kernel', 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m')
                self._push_unique(rows, seen, 'local', 'qwen2.5:32b-instruct-q4_k_m')
                self._push_unique(rows, seen, 'local', 'qwen-2.5-7b-instruct')
            else:
                self._push_unique(rows, seen, 'ai_kernel', 'hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m')
                self._push_unique(rows, seen, 'local', 'qwen2.5:32b-instruct-q4_k_m')
                self._push_unique(rows, seen, 'local', 'qwen-2.5-7b-instruct')
            if task_type in {'code', 'fix', 'test', 'review'}:
                self._push_unique(rows, seen, 'mistral', 'codestral-latest')
                self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
                self._push_unique(rows, seen, 'openai', 'gpt-5.4')
                self._push_unique(rows, seen, 'openai', 'gpt-5.5')
            else:
                self._push_unique(rows, seen, 'mistral', 'mistral-medium-latest')
                self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
                self._push_unique(rows, seen, 'openai', 'gpt-5.4-mini')
                self._push_unique(rows, seen, 'openai', 'gpt-5.4')
        elif provider == 'mistral':
            if task_type in {'code', 'fix', 'test'}:
                self._push_unique(rows, seen, 'mistral', 'codestral-latest')
                self._push_unique(rows, seen, 'mistral', 'devstral-latest')
            self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
            self._push_unique(rows, seen, 'mistral', 'mistral-medium-latest')
            self._push_unique(rows, seen, 'openai', 'gpt-5.4' if task_type in {'code', 'fix', 'test', 'review'} else 'gpt-5.4-mini')
        elif provider == 'github-copilot':
            self._push_unique(rows, seen, 'openai', 'gpt-5.4')
            self._push_unique(rows, seen, 'openai', 'gpt-5.5')
            self._push_unique(rows, seen, 'mistral', 'codestral-latest')
            self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
        elif provider == 'antigravity':
            self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
            self._push_unique(rows, seen, 'openai', 'gpt-5.4')
            self._push_unique(rows, seen, 'local', 'qwen2.5:32b-instruct-q4_k_m')
        elif provider == 'openai':
            self._push_unique(rows, seen, 'openai', 'gpt-5.4-mini')
            self._push_unique(rows, seen, 'openai', 'gpt-5.4')
            self._push_unique(rows, seen, 'openai', 'gpt-5.5')
            self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
        else:
            self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
            self._push_unique(rows, seen, 'openai', 'gpt-5.4')
            self._push_unique(rows, seen, 'local', 'qwen2.5:32b-instruct-q4_k_m')

        if reason in {'embedding_model', 'invalid_model', 'labs_not_enabled'}:
            self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
            self._push_unique(rows, seen, 'mistral', 'mistral-medium-latest')
        if high_risk:
            self._push_unique(rows, seen, 'openai', 'gpt-5.5')
            self._push_unique(rows, seen, 'openai', 'gpt-5.4')
            self._push_unique(rows, seen, 'mistral', 'mistral-large-latest')
        return [row for row in rows if row['model_name'] != model_name or row['provider'] != provider]

    def _available_pairs(self, participation: dict[str, Any]) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for bucket_name in ('active_now', 'available_but_not_wired_directly'):
            for row in participation.get(bucket_name, []):
                if not isinstance(row, dict):
                    continue
                provider = self._normalize_provider(str(row.get('provider') or ''))
                model_name = str(row.get('model_name') or '').strip()
                if not provider or not model_name or '/' in model_name:
                    continue
                pairs.add((provider, model_name))
        return pairs

    def recommend_replacement(
        self,
        task: Any,
        provider: str,
        model_name: str,
        participation: dict[str, Any],
        *,
        failure_reason: str | None = None,
    ) -> dict[str, Any] | None:
        provider = self._normalize_provider(provider)
        model_name = str(model_name or '').strip()
        if not provider or not model_name:
            return None

        current = self.decision(provider, model_name)
        unusable_reason = None
        for row in participation.get('present_but_unusable', []):
            if not isinstance(row, dict):
                continue
            row_provider = self._normalize_provider(str(row.get('provider') or ''))
            row_model = str(row.get('model_name') or '').strip()
            if row_provider == provider and row_model == model_name:
                unusable_reason = str(row.get('reason') or '').strip().lower() or None
                break

        if not (current.get('replacement_due') or unusable_reason or failure_reason):
            return None

        available_pairs = self._available_pairs(participation)
        for candidate in self._candidate_pairs(provider, model_name, task=task, failure_reason=failure_reason or unusable_reason):
            key = (self._normalize_provider(candidate['provider']), candidate['model_name'])
            if key not in available_pairs:
                continue
            return {
                'action': 'replace_model',
                'reason': failure_reason or unusable_reason or current.get('last_error_type') or 'model_unusable',
                'from_provider': provider,
                'from_model_name': model_name,
                'provider': key[0],
                'model_name': key[1],
                'same_provider': key[0] == provider,
                'policy_state': current,
                'task_type': self._task_type(task),
            }
        return None

    def build_snapshot(self, participation: dict[str, Any]) -> dict[str, Any]:
        unusable_rows: list[dict[str, Any]] = []
        for row in participation.get('present_but_unusable', []):
            if not isinstance(row, dict):
                continue
            provider = self._normalize_provider(str(row.get('provider') or ''))
            model_name = str(row.get('model_name') or '').strip()
            reason = str(row.get('reason') or '').strip().lower() or None
            recommendation = self.recommend_replacement(
                type('SyntheticTask', (), {'type': type('T', (), {'value': 'code'})(), 'complexity': type('C', (), {'value': 'medium'})(), 'priority': type('P', (), {'value': 'normal'})(), 'input': type('I', (), {'description': model_name, 'constraints': []})()})(),
                provider,
                model_name,
                participation,
                failure_reason=reason,
            )
            entry = dict(row)
            entry['policy_state'] = self.decision(provider, model_name)
            if recommendation:
                entry['replacement'] = recommendation
            unusable_rows.append(entry)
        return {
            **self.failure_snapshot(),
            'interchangeability': {
                'local_code': ['hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m', 'qwen2.5:32b-instruct-q4_k_m', 'qwen-2.5-7b-instruct', 'codestral-latest', 'mistral-large-latest', 'gpt-5.4', 'gpt-5.5'],
                'local_docs': ['qwen-2.5-7b-instruct', 'qwen2.5:32b-instruct-q4_k_m', 'mistral-medium-latest', 'mistral-large-latest', 'gpt-5.4-mini', 'gpt-5.4'],
                'cloud_code': ['codestral-latest', 'devstral-latest', 'mistral-large-latest', 'gpt-5.4', 'gpt-5.5'],
                'cloud_review': ['mistral-large-latest', 'gpt-5.4', 'gpt-5.5'],
            },
            'present_but_unusable': unusable_rows,
        }
