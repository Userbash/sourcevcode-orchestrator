from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_routing_policy import ModelRoutingPolicy

_ROLE_NAMES = (
    "code_parallel",
    "review_primary",
    "plan_primary",
    "test_primary",
    "docs_primary",
    "research_primary",
)

_BAD_STATUS_REASONS = {
    "blocked",
    "auth_failed",
    "billing_blocked",
    "provider_unavailable",
    "runtime_incompatible",
    "non_chat_incompatible",
    "probe_failed",
    "server_error",
}


@dataclass(slots=True)
class HealthPolicy:
    healthy_probe_interval_sec: int = 1800
    partial_probe_interval_sec: int = 600
    degraded_probe_interval_sec: int = 600
    blocked_probe_interval_sec: int = 3600
    local_probe_interval_sec: int = 60
    stale_after_sec: int = 7200


class ModelHealthRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.getenv("MODEL_HEALTH_REGISTRY_PATH", "core/.cache/model_health_registry.json"))
        self.policy = HealthPolicy(
            healthy_probe_interval_sec=self._read_int("AI_BRIDGE_MODEL_HEALTH_HEALTHY_SEC", 1800),
            partial_probe_interval_sec=self._read_int("AI_BRIDGE_MODEL_HEALTH_PARTIAL_SEC", 600),
            degraded_probe_interval_sec=self._read_int("AI_BRIDGE_MODEL_HEALTH_DEGRADED_SEC", 600),
            blocked_probe_interval_sec=self._read_int("AI_BRIDGE_MODEL_HEALTH_BLOCKED_SEC", 3600),
            local_probe_interval_sec=self._read_int("AI_BRIDGE_MODEL_HEALTH_LOCAL_SEC", 60),
            stale_after_sec=self._read_int("AI_BRIDGE_MODEL_HEALTH_STALE_SEC", 7200),
        )

    @staticmethod
    def _read_int(name: str, default: int) -> int:
        raw = str(os.getenv(name, str(default)) or str(default)).strip()
        try:
            return max(1, int(raw))
        except ValueError:
            return default

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        lowered = str(provider or "").strip().lower()
        if lowered in {"local", "ollama"}:
            return "local_llm"
        return lowered

    @staticmethod
    def _normalize_model(model_name: str) -> str:
        return str(model_name or "").strip()

    @staticmethod
    def _env_set(name: str) -> set[str]:
        raw = str(os.getenv(name, "") or "").strip()
        if not raw:
            return set()
        return {item.strip() for item in raw.split(",") if item.strip()}

    @staticmethod
    def _task_tier(cost_usd_per_2k: float) -> str:
        if cost_usd_per_2k <= 0.05:
            return "economy"
        if cost_usd_per_2k <= 0.35:
            return "balanced"
        return "premium"

    @staticmethod
    def _provider_rank(provider: str) -> int:
        order = {"openai": 0, "mistral": 1, "antigravity": 2, "mimo": 3, "ai_kernel": 4, "local_llm": 5}
        return order.get(str(provider or "").strip().lower(), 99)

    @classmethod
    def _infer_roles(cls, model_name: str, provider: str) -> list[str]:
        normalized = str(model_name or "").strip().lower()
        provider_n = cls._normalize_provider(provider)
        if provider_n in {"local_llm", "ai_kernel"}:
            if "7b" in normalized:
                return ["docs_primary", "plan_primary", "test_primary"]
            return ["code_parallel", "test_primary", "review_primary", "plan_primary"]
        if normalized.startswith("gpt-5.5") or "claude-opus" in normalized:
            return ["code_parallel", "review_primary", "research_primary", "plan_primary"]
        if normalized.startswith("gpt-5.4-mini") or "flash" in normalized or "haiku" in normalized:
            return ["docs_primary", "test_primary", "plan_primary"]
        if "deepseek" in normalized or "qwen" in normalized or "sonnet" in normalized:
            return ["code_parallel", "review_primary", "test_primary", "plan_primary"]
        return ["plan_primary", "docs_primary"]

    @classmethod
    def _choose_interval_sec(cls, provider: str, status: str, workable: bool, policy: HealthPolicy) -> int:
        provider_n = cls._normalize_provider(provider)
        if provider_n in {"local_llm", "ai_kernel"}:
            return policy.local_probe_interval_sec
        if status in {"ready", "routable"} and workable:
            return policy.healthy_probe_interval_sec
        if status in {"partial", "rate_limited", "degraded"}:
            return policy.partial_probe_interval_sec
        if status in {"blocked", "auth_failed", "billing_blocked", "provider_unavailable", "probe_failed"}:
            return policy.blocked_probe_interval_sec
        return policy.degraded_probe_interval_sec

    @classmethod
    def _cost_per_2k(cls, model_name: str) -> float:
        return round(ModelRoutingPolicy.estimate_cost_usd(2000, model_name), 6)

    @classmethod
    def _build_record(
        cls,
        *,
        provider: str,
        model_name: str,
        visible: bool,
        workable: bool,
        routable: bool,
        status: str,
        failure_reason: str | None,
        source: str,
        checked_at: int,
        role_scores: dict[str, Any] | None = None,
        preferred_roles: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        policy: HealthPolicy,
    ) -> dict[str, Any]:
        normalized_provider = cls._normalize_provider(provider)
        model = cls._normalize_model(model_name)
        estimated_cost = cls._cost_per_2k(model)
        roles = [role for role in (preferred_roles or cls._infer_roles(model, normalized_provider)) if role in _ROLE_NAMES]
        scores = {}
        if isinstance(role_scores, dict):
            for role in _ROLE_NAMES:
                try:
                    scores[role] = float(role_scores.get(role) or 0.0)
                except (TypeError, ValueError):
                    scores[role] = 0.0
        next_check = checked_at + cls._choose_interval_sec(normalized_provider, status, workable, policy)
        effective_reason = str(failure_reason or "").strip().lower() or None
        if effective_reason in _BAD_STATUS_REASONS:
            workable = False
            routable = False
        return {
            "provider": normalized_provider,
            "model_name": model,
            "visible": bool(visible),
            "workable": bool(workable),
            "routable": bool(routable and workable),
            "status": status,
            "failure_reason": effective_reason,
            "source_of_truth": source,
            "efficiency_tier": cls._task_tier(estimated_cost),
            "task_roles": roles,
            "role_scores": scores,
            "estimated_cost_usd_per_2k": estimated_cost,
            "checked_at": int(checked_at),
            "next_check_at": int(next_check),
            "stale_after_sec": policy.stale_after_sec,
            "metadata": metadata or {},
        }

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def persist(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        return payload

    def _openai_records(self, runtime_payload: dict[str, Any], checked_at: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        models = runtime_payload.get("models") if isinstance(runtime_payload.get("models"), list) else []
        validated = runtime_payload.get("validated_models") if isinstance(runtime_payload.get("validated_models"), list) else []
        role_map = runtime_payload.get("recommended_models", {}).get("roles", {}) if isinstance(runtime_payload.get("recommended_models"), dict) else {}
        role_by_model: dict[str, list[str]] = {}
        for role, entries in role_map.items():
            if role not in _ROLE_NAMES or not isinstance(entries, list):
                continue
            for model_name in entries:
                model = self._normalize_model(str(model_name))
                if model:
                    role_by_model.setdefault(model, []).append(role)
        validated_by_model = {
            self._normalize_model(str(row.get("model") or "")): row
            for row in validated
            if isinstance(row, dict) and str(row.get("model") or "").strip()
        }
        whitelist = self._env_set("AI_BRIDGE_MODEL_WHITELIST")
        blacklist = self._env_set("AI_BRIDGE_MODEL_BLACKLIST")
        for model_name in models:
            model = self._normalize_model(str(model_name))
            row = validated_by_model.get(model, {})
            chat = row.get("chat_completions") if isinstance(row.get("chat_completions"), dict) else {}
            responses = row.get("responses") if isinstance(row.get("responses"), dict) else {}
            chat_ok = bool(chat.get("ok"))
            responses_ok = bool(responses.get("ok"))
            available = bool(row.get("available"))
            error_text = " ".join(
                str(part or "")
                for part in (
                    row.get("reason"),
                    chat.get("error"),
                    responses.get("error"),
                    chat.get("response_sample"),
                    responses.get("response_sample"),
                )
            ).strip().lower()
            status = "routable" if available else "partial" if chat_ok or responses_ok else "blocked" if row else "discovered"
            failure_reason = None
            if model in blacklist:
                status = "blocked"
                failure_reason = "suppressed"
            elif model in whitelist:
                status = "routable" if chat_ok else "partial"
            elif "auth" in error_text:
                status = "auth_failed"
                failure_reason = "auth_failed"
            elif "billing" in error_text or "quota" in error_text:
                status = "billing_blocked"
                failure_reason = "billing_blocked"
            elif "429" in error_text or "rate limit" in error_text:
                status = "rate_limited"
                failure_reason = "rate_limited"
            elif any(marker in model.lower() for marker in ("embedding", "moderation", "tts", "whisper", "image", "audio", "transcribe", "speech", "sora", "dall", "realtime")):
                status = "blocked"
                failure_reason = "non_chat_incompatible"
            elif any(marker in error_text for marker in ("unsupported model", "model is not supported", "invalid model", "does not exist", "not found", "no eligible resources")):
                status = "blocked"
                failure_reason = "runtime_incompatible"
            elif not row:
                failure_reason = "probe_missing"
            elif not (chat_ok or responses_ok):
                failure_reason = "probe_failed"
            rows.append(
                self._build_record(
                    provider="openai",
                    model_name=model,
                    visible=True,
                    workable=chat_ok or responses_ok or model in whitelist,
                    routable=available or model in whitelist,
                    status=status,
                    failure_reason=failure_reason,
                    source="runtime_probe",
                    checked_at=checked_at,
                    role_scores=row.get("role_scores") if isinstance(row.get("role_scores"), dict) else None,
                    preferred_roles=role_by_model.get(model),
                    metadata={
                        "chat_ok": chat_ok,
                        "responses_ok": responses_ok,
                        "available": available,
                    },
                    policy=self.policy,
                )
            )
        return rows

    def _provider_records(self, provider_snapshot: dict[str, Any], checked_at: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        providers = provider_snapshot.get("providers") if isinstance(provider_snapshot.get("providers"), dict) else {}
        whitelist = self._env_set("AI_BRIDGE_MODEL_WHITELIST")
        blacklist = self._env_set("AI_BRIDGE_MODEL_BLACKLIST")
        for provider, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            provider_name = self._normalize_provider(provider)
            models = entry.get("models") if isinstance(entry.get("models"), list) else []
            provider_ok = bool(entry.get("ok", bool(models)))
            suppressed = bool(entry.get("suppressed"))
            provider_status = str(entry.get("status") or ("ready" if provider_ok else "degraded")).strip().lower()
            for item in models:
                model = self._normalize_model(str(item))
                if not model:
                    continue
                status = "ready" if provider_ok and not suppressed else "degraded"
                failure_reason = None
                workable = provider_ok and not suppressed
                routable = workable and ModelRoutingPolicy.is_model_available(model)
                if model in blacklist:
                    status = "blocked"
                    failure_reason = "suppressed"
                    workable = False
                    routable = False
                elif model in whitelist:
                    workable = True
                    routable = True
                    status = "ready"
                elif suppressed:
                    status = "blocked"
                    failure_reason = "suppressed"
                    workable = False
                    routable = False
                elif provider_status not in {"ready", "ok"}:
                    failure_reason = "provider_unavailable" if not provider_ok else provider_status
                rows.append(
                    self._build_record(
                        provider=provider_name,
                        model_name=model,
                        visible=True,
                        workable=workable,
                        routable=routable,
                        status=status,
                        failure_reason=failure_reason,
                        source="provider_inventory",
                        checked_at=checked_at,
                        metadata={"provider_status": provider_status, "suppressed": suppressed},
                        policy=self.policy,
                    )
                )
        return rows

    @staticmethod
    def _merge_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        priority = {"runtime_probe": 3, "agent_report": 2, "provider_inventory": 1}
        for row in records:
            key = (str(row.get("provider") or ""), str(row.get("model_name") or ""))
            current = merged.get(key)
            if current is None:
                merged[key] = row
                continue
            current_p = priority.get(str(current.get("source_of_truth") or ""), 0)
            next_p = priority.get(str(row.get("source_of_truth") or ""), 0)
            if next_p > current_p:
                merged[key] = row
                continue
            current["visible"] = bool(current.get("visible")) or bool(row.get("visible"))
            current["workable"] = bool(current.get("workable")) or bool(row.get("workable"))
            current["routable"] = bool(current.get("routable")) or bool(row.get("routable"))
            current["task_roles"] = sorted(set(list(current.get("task_roles") or []) + list(row.get("task_roles") or [])))
            if not current.get("failure_reason"):
                current["failure_reason"] = row.get("failure_reason")
        return sorted(merged.values(), key=lambda item: (ModelHealthRegistry._provider_rank(item.get("provider")), item.get("model_name") or ""))

    def build_registry(
        self,
        *,
        provider_snapshot: dict[str, Any],
        runtime_inventory: dict[str, Any] | None = None,
        agent_reports: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checked_at = int(time.time())
        rows = self._provider_records(provider_snapshot, checked_at)
        if isinstance(runtime_inventory, dict):
            rows.extend(self._openai_records(runtime_inventory, checked_at))
        merged_rows = self._merge_records(rows)
        roles: dict[str, list[str]] = {role: [] for role in _ROLE_NAMES}
        for row in merged_rows:
            if not bool(row.get("routable")):
                continue
            for role in row.get("task_roles") or []:
                if role in roles:
                    roles[role].append(str(row.get("model_name") or ""))
        for role, models in roles.items():
            roles[role] = [
                row.get("model_name")
                for row in sorted(
                    [item for item in merged_rows if item.get("model_name") in set(models)],
                    key=lambda item: (
                        0 if item.get("efficiency_tier") == "balanced" else 1 if item.get("efficiency_tier") == "economy" else 2,
                        float(item.get("estimated_cost_usd_per_2k") or 0.0),
                        item.get("model_name") or "",
                    ),
                )
            ]
        payload = {
            "generated_at": checked_at,
            "policy": {
                "healthy_probe_interval_sec": self.policy.healthy_probe_interval_sec,
                "partial_probe_interval_sec": self.policy.partial_probe_interval_sec,
                "degraded_probe_interval_sec": self.policy.degraded_probe_interval_sec,
                "blocked_probe_interval_sec": self.policy.blocked_probe_interval_sec,
                "local_probe_interval_sec": self.policy.local_probe_interval_sec,
                "stale_after_sec": self.policy.stale_after_sec,
                "working_model_criteria": {
                    "visible": True,
                    "workable": True,
                    "routable": True,
                    "not_blacklisted": True,
                    "provider_ready_or_probe_ok": True,
                },
                "failed_model_report_reasons": sorted(_BAD_STATUS_REASONS | {"rate_limited", "suppressed", "probe_missing"}),
            },
            "blacklist": sorted(self._env_set("AI_BRIDGE_MODEL_BLACKLIST")),
            "whitelist": sorted(self._env_set("AI_BRIDGE_MODEL_WHITELIST")),
            "agent_reports": agent_reports if isinstance(agent_reports, dict) else {},
            "models": merged_rows,
            "summary": {
                "visible_count": sum(1 for row in merged_rows if bool(row.get("visible"))),
                "workable_count": sum(1 for row in merged_rows if bool(row.get("workable"))),
                "routable_count": sum(1 for row in merged_rows if bool(row.get("routable"))),
                "blocked_count": sum(1 for row in merged_rows if str(row.get("status") or "") in {"blocked", "auth_failed", "billing_blocked"}),
            },
            "roles": roles,
        }
        return payload

    def refresh(
        self,
        *,
        provider_snapshot: dict[str, Any],
        runtime_inventory: dict[str, Any] | None = None,
        agent_reports: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.persist(self.build_registry(
            provider_snapshot=provider_snapshot,
            runtime_inventory=runtime_inventory,
            agent_reports=agent_reports,
        ))

    def find(self, provider: str, model_name: str) -> dict[str, Any] | None:
        payload = self.load()
        models = payload.get("models") if isinstance(payload, dict) else []
        provider_n = self._normalize_provider(provider)
        model = self._normalize_model(model_name)
        for row in models if isinstance(models, list) else []:
            if not isinstance(row, dict):
                continue
            if self._normalize_provider(str(row.get("provider") or "")) == provider_n and self._normalize_model(str(row.get("model_name") or "")) == model:
                return row
        return None

    def role_models(self, role: str, *, routable_only: bool = True) -> list[str]:
        payload = self.load()
        rows = payload.get("models") if isinstance(payload, dict) else []
        matched = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if role not in list(row.get("task_roles") or []):
                continue
            if routable_only and not bool(row.get("routable")):
                continue
            matched.append(row)
        matched.sort(key=lambda item: (float(item.get("estimated_cost_usd_per_2k") or 0.0), item.get("model_name") or ""))
        return [str(item.get("model_name") or "") for item in matched if str(item.get("model_name") or "").strip()]
