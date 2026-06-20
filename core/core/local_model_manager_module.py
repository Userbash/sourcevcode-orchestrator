from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .kernel_protocol import KernelAPI, KernelModule
from .local_model_memory_policy import LocalModelMemoryPolicy
from .local_model_runtime import LocalModelRuntime, LocalModelResidentInfo, LocalModelRuntimeConfig

OOM_ERROR_MARKERS = (
    "signal: killed",
    "out of memory",
    "oom",
    "cuda out of memory",
    "llama-server process has terminated",
)


@dataclass(slots=True)
class ResidentModelRecord:
    provider: str
    model_name: str
    estimated_memory_gb: float
    actual_memory_gb: float = 0.0
    resident: bool = False
    active_tasks: int = 0
    warmups: int = 0
    unloads: int = 0
    switches: int = 0
    oom_failures: int = 0
    last_used_at: str | None = None
    last_warm_at: str | None = None
    cooldown_until: str | None = None
    last_error: str | None = None
    last_action: str = "observed"
    readiness_level: str = "cold"


class LocalModelManagerModule(KernelModule):
    name = "local_model_manager"

    def __init__(self, runtime: LocalModelRuntime | None = None, policy: LocalModelMemoryPolicy | None = None) -> None:
        self._api: KernelAPI | None = None
        self.runtime = runtime or LocalModelRuntime(LocalModelRuntimeConfig.from_env())
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], ResidentModelRecord] = {}
        self._task_claims: dict[str, tuple[str, str]] = {}
        self.policy = policy or LocalModelMemoryPolicy.from_env()
        self._register_configured_models()

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        raw = str(provider or "").strip().lower()
        if raw in {"local", "local_llm", "ollama"}:
            return "local"
        if raw in {"ai-kernel", "ai_kernel", "llama_cpp", "llama-cpp"}:
            return "ai_kernel"
        return raw

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @classmethod
    def _parse_ts(cls, raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    def _register_configured_models(self) -> None:
        self._touch("local", os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL") or "qwen2.5:32b-instruct-q4_k_m")
        self._touch("local", "qwen-2.5-7b-instruct")
        self._touch("ai_kernel", os.getenv("AI_KERNEL_MODEL_ALIAS") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m")

    def _estimated_memory_gb(self, model_name: str) -> float:
        return self.policy.estimated_memory_gb(model_name)

    def _touch(self, provider: str, model_name: str) -> ResidentModelRecord:
        key = (self._normalize_provider(provider), str(model_name or "").strip())
        record = self._records.get(key)
        if record is None:
            record = ResidentModelRecord(
                provider=key[0],
                model_name=key[1],
                estimated_memory_gb=self._estimated_memory_gb(key[1]),
            )
            self._records[key] = record
        return record

    @staticmethod
    def _resident_size_gb(resident: LocalModelResidentInfo) -> float:
        size_bytes = resident.size_vram or resident.size or 0
        return round(float(size_bytes) / (1024 ** 3), 3) if size_bytes else 0.0

    def _resident_memory_gb_locked(self) -> float:
        total = 0.0
        for record in self._records.values():
            if record.provider != "local" or not record.resident:
                continue
            total += max(record.actual_memory_gb, record.estimated_memory_gb)
        return round(total, 3)

    def _sync_local_residents_locked(self) -> None:
        seen: set[str] = set()
        try:
            residents = self.runtime.list_resident_models_sync()
        except Exception as exc:
            if self._api is not None:
                self._api.log("warning", f"[LOCAL_MODEL_MANAGER] resident probe failed: {exc}")
            return

        for resident in residents:
            record = self._touch("local", resident.name)
            record.resident = True
            record.actual_memory_gb = self._resident_size_gb(resident)
            record.readiness_level = "hot"
            record.last_action = "resident_probe"
            seen.add(record.model_name)

        for (provider, model_name), record in self._records.items():
            if provider != "local":
                continue
            if model_name not in seen and record.active_tasks == 0:
                record.resident = False
                record.actual_memory_gb = 0.0
                if record.readiness_level == "hot":
                    record.readiness_level = "warm"

    def _is_in_cooldown(self, record: ResidentModelRecord) -> bool:
        cooldown_until = self._parse_ts(record.cooldown_until)
        return bool(cooldown_until and cooldown_until > self._now())

    def _record_failure_locked(self, provider: str, model_name: str, error: str, *, dedupe_window_sec: int = 5) -> dict[str, Any]:
        record = self._touch(provider, model_name)
        now = self._now()
        signature = f"{self._normalize_provider(provider)}::{model_name}::{error.strip().lower()}"
        recent_duplicate = record.last_error == signature and record.last_used_at is not None
        if recent_duplicate:
            last_used = self._parse_ts(record.last_used_at)
            if last_used and (now - last_used).total_seconds() <= dedupe_window_sec:
                return {
                    "provider": record.provider,
                    "model_name": record.model_name,
                    "oom_detected": self._looks_like_oom(error),
                    "unloaded": False,
                    "cooldown_until": record.cooldown_until,
                }
        record.last_error = signature
        record.last_used_at = now.isoformat()
        record.last_action = "failure"
        oom_detected = self._looks_like_oom(error)
        unloaded = False
        if oom_detected:
            record.oom_failures += 1
            record.cooldown_until = (now + timedelta(seconds=self.policy.oom_cooldown_sec)).isoformat()
            if record.provider == "local":
                unloaded = self._unload_local_model_locked(model_name, reason="oom_failure")
        return {
            "provider": record.provider,
            "model_name": record.model_name,
            "oom_detected": oom_detected,
            "unloaded": unloaded,
            "cooldown_until": record.cooldown_until,
        }

    @staticmethod
    def _looks_like_oom(error: str) -> bool:
        lowered = str(error or "").strip().lower()
        return any(marker in lowered for marker in OOM_ERROR_MARKERS)

    def _unload_local_model_locked(self, model_name: str, *, reason: str) -> bool:
        record = self._touch("local", model_name)
        try:
            self.runtime.unload_model_sync(model_name)
        except Exception as exc:
            record.last_error = str(exc)
            record.last_action = f"unload_failed:{reason}"
            return False
        record.resident = False
        record.actual_memory_gb = 0.0
        record.readiness_level = "warm"
        record.last_action = f"unloaded:{reason}"
        record.unloads += 1
        return True

    def _warm_local_model_locked(self, model_name: str) -> bool:
        record = self._touch("local", model_name)
        if self._is_in_cooldown(record):
            record.last_action = "cooldown_skip"
            return False
        try:
            result = self.runtime.warm_model_sync(model_name, keep_alive=self.policy.warm_keep_alive_sec, timeout_sec=max(1.0, self.runtime.config.health_timeout_sec))
        except Exception as exc:
            record.last_error = str(exc)
            record.last_action = "warm_failed"
            return False
        record.resident = True
        record.warmups += 1
        record.last_warm_at = self._now().isoformat()
        record.last_action = "warmed"
        record.readiness_level = "hot"
        load_memory = self._resident_size_gb(LocalModelResidentInfo(name=model_name, size_vram=result.payload.get("size_vram") if isinstance(result.payload, dict) else None))
        if load_memory > 0:
            record.actual_memory_gb = load_memory
        return True

    def _eviction_candidates_locked(self, exclude_model: str = "") -> list[ResidentModelRecord]:
        rows: list[ResidentModelRecord] = []
        for record in self._records.values():
            if record.provider != "local" or not record.resident or record.active_tasks > 0:
                continue
            if exclude_model and record.model_name == exclude_model:
                continue
            rows.append(record)
        rows.sort(key=lambda item: item.last_used_at or "")
        return rows

    def _evict_idle_locked(self) -> list[str]:
        unloaded: list[str] = []
        threshold = self._now() - timedelta(seconds=self.policy.idle_unload_sec)
        for record in self._eviction_candidates_locked():
            last_used = self._parse_ts(record.last_used_at)
            last_warm = self._parse_ts(record.last_warm_at)
            reference = last_used or last_warm
            if reference and reference > threshold:
                continue
            if self._unload_local_model_locked(record.model_name, reason="idle"):
                unloaded.append(record.model_name)
        return unloaded

    def _ensure_capacity_locked(self, provider: str, model_name: str) -> list[str]:
        unloaded: list[str] = []
        normalized = self._normalize_provider(provider)
        if normalized not in {"local", "ai_kernel"}:
            return unloaded
        requested = self._estimated_memory_gb(model_name)
        budget_limit = self.policy.total_memory_budget_gb * self.policy.pressure_threshold
        while self._resident_memory_gb_locked() + requested > budget_limit:
            candidates = self._eviction_candidates_locked(exclude_model=model_name if normalized == "local" else "")
            if not candidates:
                break
            victim = candidates[0]
            if not self._unload_local_model_locked(victim.model_name, reason=f"pressure_for:{model_name}"):
                break
            victim.switches += 1
            unloaded.append(victim.model_name)
        return unloaded

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        with self._lock:
            self._sync_local_residents_locked()
        api.log("info", "[LOCAL_MODEL_MANAGER] loaded")

    def on_unload(self) -> None:
        self._api = None

    def prepare_for_task(self, provider: str, model_name: str, *, task_id: str | None = None) -> dict[str, Any]:
        normalized = self._normalize_provider(provider)
        target_model = str(model_name or "").strip()
        if normalized not in {"local", "ai_kernel"} or not target_model:
            return self.finalize()
        with self._lock:
            self._sync_local_residents_locked()
            idle_unloaded = self._evict_idle_locked()
            pressure_unloaded = self._ensure_capacity_locked(normalized, target_model)
            warmed = False
            record = self._touch(normalized, target_model)
            if normalized == "local":
                warmed = self._warm_local_model_locked(target_model)
                self._sync_local_residents_locked()
            record.active_tasks += 1
            record.last_used_at = self._now().isoformat()
            record.readiness_level = "hot"
            record.last_action = "task_claimed"
            if task_id:
                self._task_claims[task_id] = (normalized, target_model)
            return {
                "provider": normalized,
                "model_name": target_model,
                "warmed": warmed,
                "idle_unloaded": idle_unloaded,
                "pressure_unloaded": pressure_unloaded,
                "resident_memory_gb": self._resident_memory_gb_locked(),
                "budget_limit_gb": self.policy.budget_limit_gb,
                "blocked": self._is_in_cooldown(record),
            }

    def handle_failure(self, provider: str, model_name: str, error: str, *, task_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            payload = self._record_failure_locked(provider, model_name, error)
            if task_id:
                self._task_claims.pop(task_id, None)
            self._sync_local_residents_locked()
            return payload

    def warm_model(self, model_name: str) -> bool:
        with self._lock:
            self._sync_local_residents_locked()
            self._ensure_capacity_locked('local', model_name)
            warmed = self._warm_local_model_locked(model_name)
            self._sync_local_residents_locked()
            return warmed

    def unload_model(self, model_name: str, *, reason: str = 'manual') -> bool:
        with self._lock:
            unloaded = self._unload_local_model_locked(model_name, reason=reason)
            self._sync_local_residents_locked()
            return unloaded

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        provider = str(context.get("selected_provider") or context.get("provider") or "")
        model_name = str(context.get("selected_model") or context.get("model") or "")
        snapshot = self.prepare_for_task(provider, model_name, task_id=getattr(task, "task_id", None))
        context["local_model_manager"] = snapshot

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        provider = str(getattr(result, "provider", None) or context.get("provider") or context.get("selected_provider") or "")
        model_name = str(getattr(result, "model_name", None) or context.get("model") or context.get("selected_model") or "")
        task_id = getattr(task, "task_id", None)
        with self._lock:
            claim = self._task_claims.pop(task_id, None) if task_id else None
            normalized = self._normalize_provider(provider)
            if claim is not None:
                normalized, model_name = claim
            if normalized in {"local", "ai_kernel"} and model_name:
                record = self._touch(normalized, model_name)
                record.active_tasks = max(0, record.active_tasks - 1)
                record.last_used_at = self._now().isoformat()
                if getattr(getattr(result, "status", None), "value", getattr(result, "status", "")) == "failed":
                    errors = getattr(result, "errors", None) or []
                    if errors:
                        self._record_failure_locked(normalized, model_name, "; ".join(str(item) for item in errors))
            self._sync_local_residents_locked()
            self._evict_idle_locked()

    def finalize(self) -> dict[str, Any]:
        with self._lock:
            self._sync_local_residents_locked()
            blocked_models = [
                {"provider": record.provider, "model_name": record.model_name, "cooldown_until": record.cooldown_until}
                for record in sorted(self._records.values(), key=lambda item: (item.provider, item.model_name))
                if self._is_in_cooldown(record)
            ]
            models = [asdict(record) for record in sorted(self._records.values(), key=lambda item: (item.provider, item.model_name))]
            resident_models = [item for item in models if item["resident"]]
            total_warmups = sum(int(item['warmups']) for item in models)
            total_evictions = sum(int(item['unloads']) for item in models)
            pressure = round((self._resident_memory_gb_locked() / self.policy.budget_limit_gb), 3) if self.policy.budget_limit_gb else 0.0
            return {
                "status": "ready",
                "policy": self.policy.as_dict(),
                "budget_gb": self.policy.total_memory_budget_gb,
                "pressure_threshold": self.policy.pressure_threshold,
                "resident_memory_gb": self._resident_memory_gb_locked(),
                "memory_pressure": {
                    "resident_memory_gb": self._resident_memory_gb_locked(),
                    "budget_limit_gb": self.policy.budget_limit_gb,
                    "pressure_ratio": pressure,
                    "pressure_state": 'high' if pressure >= 1.0 else 'elevated' if pressure >= 0.85 else 'normal',
                },
                "blocked_models": blocked_models,
                "resident_models": resident_models,
                "models": models,
                "warmups": total_warmups,
                "evictions": total_evictions,
                "active_tasks": {task_id: {"provider": provider, "model_name": model_name} for task_id, (provider, model_name) in sorted(self._task_claims.items())},
            }
