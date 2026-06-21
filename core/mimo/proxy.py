from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any

from .bridge import MimoAsyncBridge, MimoModelSnapshot
from .state import MimoStateContext
from core.core.mistral_governance import MistralGovernance
from core.core.model_value import compute_model_value, context_fit_score, memory_efficiency_score
from core.core.openai_model_registry import OpenAIModelRegistry
from core.core.qwen_model_registry import QwenModelRegistry
from core.core.mimo_status import build_mimo_runtime_status, load_mimo_ping_report, load_mimo_usable_report

logger = logging.getLogger(__name__)

MIMO_UNAVAILABLE_DECISION = "MIMO_UNAVAILABLE_DECISION"


@dataclass(slots=True)
class TaskKPIWindow:
    successes: deque[bool] = field(default_factory=lambda: deque(maxlen=20))
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    quality_scores: deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def snapshot(self) -> dict[str, float]:
        count = max(1, len(self.latencies))
        success_rate = sum(self.successes) / max(1, len(self.successes))
        avg_latency = sum(self.latencies) / count
        avg_quality = sum(self.quality_scores) / max(1, len(self.quality_scores))
        return {"success_rate": round(success_rate, 3), "avg_latency": round(avg_latency, 2), "avg_quality": round(avg_quality, 3)}


@dataclass(slots=True)
class MimoRecommendation:
    provider: str
    model_name: str
    confidence: float
    allow: bool
    reason: str
    selection_trace: list[dict[str, Any]] = field(default_factory=list)
    requires_escalation: bool = False
    escalation_reason: str | None = None
    blocked_by: str | None = None
    fallback_options: list[dict[str, str]] = field(default_factory=list)
    decision_mode: str = "mimo_control"


class MimoOrchestrationDirector:
    def __init__(self) -> None:
        self.state = MimoStateContext()
        self.bridge = MimoAsyncBridge()
        self.mistral_governance = MistralGovernance()
        self.is_available = True
        self._budget_module: Any | None = None
        self._memory_source: Any | None = None
        self._kpi_source: Any | None = None
        self._quality_source: Any | None = None
        self._history_source: Any | None = None
        self._vfs_source: Any | None = None
        self._status_source: Any | None = None
        self.profile_dir = Path(__file__).resolve().parent / "profiles"
        self.kpi_store_path = self.profile_dir / "rolling_kpi_store.json"
        self._profile_mtimes: dict[Path, float] = {}
        self.profile_manifest_path = self.profile_dir / "manifest.json"
        self.task_profiles: dict[str, dict[str, Any]] = self._load_profiles()
        self.task_kpi_windows: dict[tuple[str, str], TaskKPIWindow] = {}
        self.last_failure_reason: str | None = None
        self.recovery_attempts: int = 0
        self.last_sync_at: str | None = None
        self._load_persisted_kpi_windows()

    def _load_manifest_entries(self, manifest_path: Path) -> list[Path]:
        try:
            stat = manifest_path.stat()
            self._profile_mtimes[manifest_path] = stat.st_mtime
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(manifest, dict):
            return []
        manifest_dir = manifest_path.parent
        entries: list[Path] = []
        for section in ("task_profiles", "provider_profiles", "model_profiles", "combo_profiles"):
            values = manifest.get(section)
            if not isinstance(values, list):
                continue
            for item in values:
                path = manifest_dir / str(item)
                if path.is_file():
                    entries.append(path)
        return entries

    def _iter_profile_files(self) -> list[Path]:
        files: list[Path] = []
        seen: set[Path] = set()

        def add(path: Path) -> None:
            if path in seen or not path.is_file():
                return
            seen.add(path)
            files.append(path)

        if self.profile_manifest_path.exists():
            for path in self._load_manifest_entries(self.profile_manifest_path):
                add(path)

        generated_root = self.profile_dir / "generated"
        if generated_root.exists():
            for manifest_path in sorted(generated_root.rglob("manifest.json")):
                for path in self._load_manifest_entries(manifest_path):
                    add(path)

        if files:
            return files

        for file in self.profile_dir.rglob("*.json"):
            if file.name in {"rolling_kpi_store.json", "manifest.json"}:
                continue
            add(file)
        return files

    def set_budget_module(self, budget_module: Any | None) -> None:
        self._budget_module = budget_module

    def set_memory_source(self, memory_source: Any | None) -> None:
        self._memory_source = memory_source

    def set_kpi_source(self, kpi_source: Any | None) -> None:
        self._kpi_source = kpi_source

    def set_quality_source(self, quality_source: Any | None) -> None:
        self._quality_source = quality_source

    def set_history_source(self, history_source: Any | None) -> None:
        self._history_source = history_source

    def set_vfs_source(self, vfs_source: Any | None) -> None:
        self._vfs_source = vfs_source

    def set_status_source(self, status_source: Any | None) -> None:
        self._status_source = status_source

    def antigravity_snapshot(self) -> dict[str, Any]:
        if self._status_source is None:
            return {}
        try:
            snapshot = self._status_source()
            return snapshot if isinstance(snapshot, dict) else {"value": snapshot}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def status_snapshot(self) -> dict[str, Any]:
        snapshot = build_mimo_runtime_status(
            bridge=self.bridge,
            status_source_configured=self._status_source is not None,
            failure_reason=self.last_failure_reason,
            recovery_attempts=self.recovery_attempts,
            last_sync_at=self.last_sync_at,
            profiles_loaded=len(self.task_profiles),
        )
        snapshot["mimo_available"] = bool(self.is_available)
        snapshot["runtime_ready"] = bool(self._runtime_health().get("ready"))
        snapshot["selection_mode"] = "mimo_control" if self.is_available else "safe_fallback"
        return snapshot

    def _task_key(self, task: Any, model_name: str) -> tuple[str, str]:
        task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
        return task_type, model_name

    def _task_text(self, task: Any) -> str:
        description = getattr(getattr(task, "input", None), "description", "") or ""
        constraints = getattr(getattr(task, "input", None), "constraints", []) or []
        criteria = getattr(getattr(task, "input", None), "acceptance_criteria", []) or []
        parts = [str(description)] + [str(item) for item in constraints] + [str(item) for item in criteria]
        return " ".join(parts).lower()

    def _task_priority(self, task: Any) -> str:
        return str(getattr(getattr(task, "priority", None), "value", None) or getattr(task, "priority", "normal")).lower()

    def _profile_name_candidates(self, task_type: str, task: Any | None = None, context: dict[str, Any] | None = None) -> list[str]:
        normalized = str(task_type or "unknown").lower().strip()
        candidates: list[str] = []
        context = context or {}
        task_text = self._task_text(task) if task is not None else ""
        task_priority = self._task_priority(task) if task is not None else "normal"
        quality_min = float(context.get("quality_min_confidence") or 0.0)
        budget_pressure = str(context.get("budget_pressure") or "normal").lower()
        vfs_pressure = str(context.get("vfs_pressure") or "normal").lower()
        provider = str(context.get("selected_provider") or context.get("provider") or "").lower().strip()
        model_name = str(context.get("selected_model") or context.get("requested_model") or "").lower().strip()

        if normalized == "plan":
            if task_priority in {"high", "critical"} or quality_min >= 0.88:
                candidates.append("plan_critical")
            if task_priority == "high" or quality_min >= 0.8:
                candidates.append("plan_high")
            if "research" in task_text or "investigat" in task_text or "analysis" in task_text:
                candidates.append("plan_research")
        elif normalized == "code":
            if task_priority in {"high", "critical"} or quality_min >= 0.82:
                candidates.append("code_senior")
            if budget_pressure == "high" or vfs_pressure in {"high", "medium"} or any(token in task_text for token in ("perf", "optimiz", "hot path", "refactor")):
                candidates.append("code_fast")
            if any(token in task_text for token in ("bug", "fix", "hotfix", "patch")):
                candidates.append("code_fix")
            if any(token in task_text for token in ("refactor", "rewrite", "cleanup", "moderniz")):
                candidates.append("code_refactor")
        elif normalized == "test":
            if task_priority in {"high", "critical"}:
                candidates.append("test_critical")
            if any(token in task_text for token in ("regression", "flaky", "failure", "broken", "rerun")):
                candidates.append("test_regression")
        elif normalized == "review":
            if any(token in task_text for token in ("security", "auth", "rbac", "audit", "compliance")):
                candidates.append("review_security")
            if task_priority in {"high", "critical"} or quality_min >= 0.82:
                candidates.append("review_senior")
        elif normalized == "docs":
            if any(token in task_text for token in ("api", "sdk", "reference", "endpoint")):
                candidates.append("docs_api")
            if any(token in task_text for token in ("release", "changelog", "migration", "rollout")):
                candidates.append("docs_release")
            if budget_pressure == "high" or vfs_pressure == "high":
                candidates.append("docs_light")
        elif normalized == "fix":
            if any(token in task_text for token in ("regression", "test", "crash", "error")):
                candidates.append("fix_regression")
            candidates.append("fix")
        elif normalized == "research":
            if any(token in task_text for token in ("market", "benchmark", "compare", "survey")):
                candidates.append("research_compare")
            candidates.append("research_deep")

        if provider and model_name:
            candidates.append(f"combo::{provider}::{model_name}")
        if provider:
            candidates.append(f"provider::{provider}")
        if model_name:
            candidates.append(f"model::{model_name}")

        candidates.append(normalized)
        seen: set[str] = set()
        ordered: list[str] = []
        for candidate in candidates:
            if candidate not in seen:
                ordered.append(candidate)
                seen.add(candidate)
        return ordered

    def _window_for(self, task_type: str, model_name: str) -> TaskKPIWindow:
        key = (task_type, model_name)
        window = self.task_kpi_windows.get(key)
        if window is None:
            window = TaskKPIWindow()
            self.task_kpi_windows[key] = window
        return window

    def _record_window(self, task: Any, model_name: str, is_successful: bool, latency: float, quality_score: float) -> None:
        task_type, model = self._task_key(task, model_name)
        window = self._window_for(task_type, model)
        window.successes.append(bool(is_successful))
        window.latencies.append(max(0.0, float(latency)))
        window.quality_scores.append(max(0.0, min(1.0, float(quality_score))))
        self._decay_window(window)
        self._persist_kpi_windows()

    def _decay_window(self, window: TaskKPIWindow, decay: float = 0.97) -> None:
        if len(window.latencies) <= 1:
            return
        window.latencies = deque((value * decay for value in window.latencies), maxlen=window.latencies.maxlen)
        window.quality_scores = deque((min(1.0, value * decay + (1.0 - decay) * 0.5) for value in window.quality_scores), maxlen=window.quality_scores.maxlen)

    def _profile_weights(self, task_type: str, provider: str, model_name: str, task: Any | None = None, context: dict[str, Any] | None = None, profile: dict[str, Any] | None = None) -> dict[str, float]:
        profile = profile if isinstance(profile, dict) else self._profile(task_type, task=task, context=context)
        weights = {"budget": 1.0, "quality": 1.0, "vfs": 1.0}
        provider_weights = profile.get("provider_weights") if isinstance(profile.get("provider_weights"), dict) else {}
        model_weights = profile.get("model_class_weights") if isinstance(profile.get("model_class_weights"), dict) else {}
        provider_norm = provider.lower().strip()
        model_norm = model_name.lower().strip()
        if provider_norm in provider_weights:
            pw = provider_weights[provider_norm]
            if isinstance(pw, dict):
                weights["quality"] *= float(pw.get("quality", 1.0))
                weights["budget"] *= float(pw.get("budget", 1.0))
                weights["vfs"] *= float(pw.get("vfs", 1.0))
        for token, key in (("qwen", "qwen"), ("gpt", "gpt")):
            if token in model_norm and key in model_weights:
                mw = model_weights[key]
                if isinstance(mw, dict):
                    weights["quality"] *= float(mw.get("quality", 1.0))
                    weights["budget"] *= float(mw.get("budget", 1.0))
        return weights

    def _rolling_kpi(self, task: Any, model_name: str) -> dict[str, float]:
        task_type, model = self._task_key(task, model_name)
        window = self.task_kpi_windows.get((task_type, model))
        if window is None:
            return {"success_rate": 0.5, "avg_latency": 0.0, "avg_quality": 0.5, "sample_size": 0.0}
        snap = window.snapshot()
        snap["sample_size"] = float(len(window.latencies))
        return snap

    def _context_depth_for(self, task: Any, context: dict[str, Any], model_name: str, profile: dict[str, Any] | None = None) -> int:
        task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
        profile = profile if isinstance(profile, dict) else self._profile(task_type)
        profile_weights = self._profile_weights(task_type, str(context.get("selected_provider") or "local"), model_name, task=task, context=context, profile=profile)
        budget_pressure = str(context.get("budget_pressure") or "normal")
        vfs_pressure = str(context.get("vfs_pressure") or "normal")
        quality_min = float(context.get("quality_min_confidence") or 0.0)
        task_priority = str(getattr(getattr(task, "priority", None), "value", None) or getattr(task, "priority", "normal")).lower()
        rolling = self._rolling_kpi(task, model_name)
        depth = int(profile.get("default_context_depth") or 1)
        depth += 1 if profile_weights.get("quality", 1.0) > 1.1 else 0
        depth -= 1 if budget_pressure == "high" else 0
        depth -= 1 if vfs_pressure in {"high", "medium"} else 0
        depth += 1 if rolling.get("success_rate", 0.5) < 0.65 and task_type in {"plan", "review"} else 0
        depth += 1 if task_priority in {"high", "critical"} else 0
        depth += 1 if quality_min >= 0.75 and task_type in {"plan", "review"} else 0
        depth += 1 if quality_min >= 0.85 and task_type in {"plan", "review"} else 0
        depth -= 1 if quality_min < 0.65 and task_type in {"docs", "test"} else 0
        return max(1, min(6, depth))

    def _load_profiles(self) -> dict[str, dict[str, Any]]:
        profiles: dict[str, dict[str, Any]] = {}
        if not self.profile_dir.exists():
            return profiles
        files = self._iter_profile_files()
        for file in files:
            if file.name == "rolling_kpi_store.json" or file.name == "manifest.json":
                continue
            try:
                stat = file.stat()
                self._profile_mtimes[file] = stat.st_mtime
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception:
                continue
            profile_key = str(data.get("profile_key") or data.get("task_type") or file.stem).lower()
            profiles[profile_key] = data
        return profiles

    def _profile(self, task_type: str, task: Any | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        for candidate in self._profile_name_candidates(task_type, task=task, context=context):
            profile = self.task_profiles.get(candidate)
            if profile:
                return profile
        return self.task_profiles.get(task_type, {})

    def reload_profiles_if_changed(self) -> bool:
        if not self.profile_dir.exists():
            return False
        changed = False
        current_profiles: dict[str, dict[str, Any]] = {}
        current_mtimes: dict[Path, float] = {}
        files = self._iter_profile_files()
        manifest_paths = [self.profile_manifest_path]
        generated_root = self.profile_dir / "generated"
        if generated_root.exists():
            manifest_paths.extend(sorted(generated_root.rglob("manifest.json")))
        for manifest_path in manifest_paths:
            if not manifest_path.exists():
                continue
            try:
                stat = manifest_path.stat()
            except Exception:
                continue
            current_mtimes[manifest_path] = stat.st_mtime
            if self._profile_mtimes.get(manifest_path) != stat.st_mtime:
                changed = True
        for file in files:
            if file.name in {"rolling_kpi_store.json", "manifest.json"}:
                continue
            try:
                stat = file.stat()
                current_mtimes[file] = stat.st_mtime
                if self._profile_mtimes.get(file) == stat.st_mtime:
                    continue
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception:
                continue
            profile_key = str(data.get("profile_key") or data.get("task_type") or file.stem).lower()
            current_profiles[profile_key] = data
            changed = True
        if changed:
            self.task_profiles.update(current_profiles)
            self._profile_mtimes = current_mtimes
        return changed

    def _load_persisted_kpi_windows(self) -> None:
        if not self.kpi_store_path.exists():
            return
        try:
            payload = json.loads(self.kpi_store_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        for key, data in payload.items():
            try:
                task_type, model_name = key.split("::", 1)
                window = TaskKPIWindow()
                for value in data.get("successes", []):
                    window.successes.append(bool(value))
                for value in data.get("latencies", []):
                    window.latencies.append(float(value))
                for value in data.get("quality_scores", []):
                    window.quality_scores.append(float(value))
                self.task_kpi_windows[(task_type, model_name)] = window
            except Exception:
                continue

    def _persist_kpi_windows(self) -> None:
        try:
            self.kpi_store_path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {}
            for (task_type, model_name), window in self.task_kpi_windows.items():
                payload[f"{task_type}::{model_name}"] = {
                    "successes": list(window.successes),
                    "latencies": list(window.latencies),
                    "quality_scores": list(window.quality_scores),
                }
            self.kpi_store_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("MIMO KPI persistence skipped: %s", exc)

    @staticmethod
    def _normalize_provider_name(provider: str | None) -> str:
        normalized = str(provider or "local").strip().lower()
        if normalized in {"antigravity-cli", "agy", "gemini", "gemini-cli", "google"}:
            return "antigravity"
        if normalized in {"local_llm", "ollama"}:
            return "local"
        if normalized in {"mimo", "mimo-cli", "xiaomi", "github-copilot", "github-models"}:
            return "mimo"
        return normalized or "local"

    @staticmethod
    def _cost_class_score(cost_class: str | None) -> float:
        normalized = str(cost_class or "").strip().lower()
        if normalized in {"low", "cheap", "budget"}:
            return 1.0
        if normalized in {"medium", "standard"}:
            return 0.7
        if normalized in {"high", "premium", "expensive"}:
            return 0.35
        return 0.8

    def _observed_model_cost(self, model_name: str, provider: str) -> dict[str, Any]:
        history = []
        if self._budget_module is not None:
            history = list(getattr(self._budget_module, "history", []) or [])
        model_norm = str(model_name or "").strip()
        provider_norm = self._normalize_provider_name(provider)
        costs: list[float] = []
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            if str(item.get("model") or "").strip() != model_norm:
                continue
            if self._normalize_provider_name(str(item.get("provider") or "local")) != provider_norm:
                continue
            costs.append(float(item.get("estimated_cost_usd") or 0.0))
            if len(costs) >= 25:
                break
        if not costs:
            return {"avg_cost_usd": 0.0, "samples": 0, "cost_efficiency": 1.0}
        avg_cost = sum(costs) / max(1, len(costs))
        return {
            "avg_cost_usd": round(avg_cost, 6),
            "samples": len(costs),
            "cost_efficiency": round(max(0.0, min(1.0, 1.0 / (1.0 + (avg_cost * 25.0)))), 4),
        }

    def _candidate_value(self, task: Any, candidate: dict[str, Any], advisory_context: dict[str, Any] | None = None) -> tuple[float, dict[str, Any]]:
        advisory_context = advisory_context or {}
        provider = str(candidate.get("provider") or "local")
        model_name = str(candidate.get("model_name") or "")
        rolling = self._rolling_kpi(task, model_name)
        observed_cost = self._observed_model_cost(model_name, provider)
        mimo_context = advisory_context.get("mimo") if isinstance(advisory_context.get("mimo"), dict) else {}
        budget_pressure = str((mimo_context or {}).get("budget_pressure") or "normal").lower()
        weights = self._profile_weights(
            getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown")),
            provider,
            model_name,
            task=task,
            context={"budget_pressure": budget_pressure},
        )
        memory_efficiency = float((mimo_context or {}).get("memory_efficiency") or 1.0)
        health = self._provider_health(advisory_context).get(provider, {})
        availability = 1.0 if not isinstance(health, dict) or not health else (1.0 if bool(health.get("ready", False)) else 0.25)
        tags = set(candidate.get("capability_tags") or [])
        specialization = 1.0 if not tags or not self._capability_tags_for_task(task).isdisjoint(tags) else 0.35
        value_payload = compute_model_value(
            success_rate=float(rolling.get("success_rate") or 0.5),
            quality_score=float(rolling.get("avg_quality") or 0.5),
            latency_ms=float(rolling.get("avg_latency") or 0.0) * 1000.0,
            cost_usd=float(observed_cost.get("avg_cost_usd") or 0.0),
            memory_efficiency=memory_efficiency,
            availability=availability,
            specialization=specialization,
            context_fit=context_fit_score(candidate.get("context_window")),
        )
        diagnostics = dict(value_payload.get("components") or {})
        diagnostics.update({
            "observed_avg_cost_usd": float(observed_cost.get("avg_cost_usd") or 0.0),
            "cost_samples": int(observed_cost.get("samples") or 0),
            "budget_weight": round(float(weights.get("budget", 1.0)), 4),
            "cost_class": str(candidate.get("cost_class") or ""),
        })
        budget_weight = max(0.7, min(1.0, float(weights.get("budget", 1.0))))
        weighted_value = float(value_payload.get("value_score") or 0.0) * budget_weight
        diagnostics["applied_budget_weight"] = round(budget_weight, 4)
        return round(min(1.0, weighted_value), 6), diagnostics

    def _provider_health(self, advisory_context: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        advisory_context = advisory_context or {}
        providers: dict[str, dict[str, Any]] = {}
        snapshot = self.antigravity_snapshot()
        if isinstance(snapshot, dict):
            raw_providers = snapshot.get("providers")
            if isinstance(raw_providers, dict):
                for provider, state in raw_providers.items():
                    if not isinstance(state, dict):
                        continue
                    normalized = dict(state)
                    status_value = str(normalized.get("status") or "").strip().lower()
                    if "ready" not in normalized:
                        normalized["ready"] = status_value in {"healthy", "ready", "ok"}
                    providers[self._normalize_provider_name(provider)] = normalized
            elif snapshot:
                normalized = dict(snapshot)
                status_value = str(normalized.get("status") or "").strip().lower()
                if "ready" not in normalized:
                    normalized["ready"] = status_value in {"healthy", "ready", "ok"}
                providers["antigravity"] = normalized
        local = advisory_context.get("local_llm")
        if isinstance(local, dict):
            providers["local"] = {
                "ready": bool(local.get("ready")),
                "status": "ready" if local.get("ready") else str(local.get("status") or "degraded"),
                "error": local.get("error"),
            }
        return providers

    @staticmethod
    def _capability_tags_for_task(task: Any) -> set[str]:
        task_type = str(getattr(getattr(task, "type", None), "value", None) or getattr(task, "type", "unknown")).lower()
        mapping = {
            "plan": {"plan", "planning", "docs", "research", "review"},
            "docs": {"docs", "documentation", "research", "review"},
            "research": {"research", "analysis", "docs", "review"},
            "review": {"review", "research", "docs"},
            "code": {"code"},
            "test": {"test", "verification"},
            "fix": {"code", "fix"},
        }
        return mapping.get(task_type, {task_type})

    def resolve_candidate_models(self, task: Any, advisory_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        advisory_context = advisory_context or {}
        provider_health = self._provider_health(advisory_context)
        capability_tags = self._capability_tags_for_task(task)
        cached_models = list(self.bridge.get_cached_models()) if hasattr(self.bridge, "get_cached_models") else []
        runtime = build_mimo_runtime_status(bridge=self.bridge)
        auth_categories = runtime.get("auth_categories", {}) if isinstance(runtime.get("auth_categories"), dict) else {}
        report = load_mimo_ping_report()
        usable = load_mimo_usable_report()
        allowed_names: set[str] = set()
        blocked_names: set[str] = set()
        for row in usable.get("models", []):
            if isinstance(row, dict):
                model_name = str(row.get("model") or "").strip()
                if model_name:
                    allowed_names.add(model_name)
                    if "/" in model_name:
                        allowed_names.add(model_name.split("/", 1)[1])
        if not allowed_names:
            for row in report.get("models", []):
                if not isinstance(row, dict):
                    continue
                model_name = str(row.get("model") or "").strip()
                if not model_name:
                    continue
                if row.get("ok"):
                    allowed_names.add(model_name)
                    if "/" in model_name:
                        allowed_names.add(model_name.split("/", 1)[1])
                else:
                    blocked_names.add(model_name)
                    if "/" in model_name:
                        blocked_names.add(model_name.split("/", 1)[1])
        candidates: list[dict[str, Any]] = []
        for model in cached_models:
            provider = self._normalize_provider_name(getattr(model, "provider", "local"))
            blocked = bool(getattr(model, "blocked", False))
            ready = getattr(model, "ready", None)
            status = str(getattr(model, "status", "")).strip().lower()
            health = provider_health.get(provider)
            health_ready = True if not isinstance(health, dict) or not health else bool(health.get("ready", False))
            tags = {str(item).strip().lower() for item in (getattr(model, "capability_tags", None) or []) if str(item).strip()}
            full_id = str(getattr(model, "full_id", "")).strip()
            short_id = str(getattr(model, "id", "") or full_id).strip()
            if blocked:
                continue
            if ready is False or status in {"offline", "error", "disabled"}:
                continue
            if not health_ready:
                continue
            if tags and capability_tags.isdisjoint(tags):
                continue
            if auth_categories.get("github_pat_not_supported") and provider == "github-copilot":
                continue
            if blocked_names and (full_id in blocked_names or short_id in blocked_names):
                continue
            if allowed_names and not ({full_id, short_id} & allowed_names):
                continue
            execution_model = full_id if provider == "mimo" and full_id else short_id
            candidate = {
                "provider": provider,
                "model_name": execution_model,
                "short_model_name": short_id,
                "full_id": full_id,
                "context_window": getattr(model, "context_window", None),
                "capability_tags": sorted(tags),
                "cost_class": getattr(model, "cost_class", None),
                "source": "mimo_inventory",
            }
            value_score, diagnostics = self._candidate_value(task, candidate, advisory_context)
            bias = self._mimo_routing_bias(candidate, str(getattr(getattr(task, "type", None), "value", None) or getattr(task, "type", "unknown")).lower())
            candidate["value_score"] = value_score + bias
            diagnostics["mimo_routing_bias"] = round(bias, 3)
            candidate["value_diagnostics"] = diagnostics
            candidates.append(candidate)
        if candidates:
            candidates.sort(key=lambda item: (-float(item.get("value_score") or 0.0), -float((item.get("value_diagnostics") or {}).get("cost_efficiency") or 0.0), -(int(item.get("context_window") or 0))))
            return candidates
        return self._fallback_options(task, advisory_context)

    @staticmethod
    def _mimo_routing_bias(candidate: dict[str, Any], task_type: str) -> float:
        model_name = str(candidate.get("full_id") or candidate.get("model_name") or "").strip().lower()
        bonus = 0.0
        if model_name.startswith("xiaomi/mimo-v2.5-pro"):
            bonus += 0.22
        elif model_name.startswith("xiaomi/mimo-v2.5"):
            bonus += 0.18
        elif model_name.startswith("xiaomi/mimo-v2-pro"):
            bonus += 0.15
        elif model_name.startswith("xiaomi/mimo-v2"):
            bonus += 0.1
        elif model_name.startswith("mimo/mimo-auto"):
            bonus += 0.08
        if task_type in {"docs", "research", "review", "plan"}:
            bonus += 0.04
        if task_type in {"code", "test", "fix"} and "pro" in model_name:
            bonus += 0.03
        return bonus

    @staticmethod
    def _default_local_model(task_type: str) -> str:
        if task_type in {"docs", "research", "review", "plan"}:
            return "qwen-2.5-7b-instruct"
        if os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
            return "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"
        return "qwen2.5:32b-instruct-q4_k_m"

    @staticmethod
    def _default_local_provider(task_type: str) -> str:
        if task_type in {"docs", "research", "review", "plan"}:
            return "local"
        if os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
            return "ai_kernel"
        return "local"

    @staticmethod
    def _unavailable_decision_event(*, mode: str, allow: bool, reason: str, provider: str, model_name: str) -> dict[str, Any]:
        return {
            "event": "decision_mode",
            "event_type": MIMO_UNAVAILABLE_DECISION,
            "mode": mode,
            "allow": allow,
            "reason": reason,
            "provider": provider,
            "model_name": model_name,
        }

    def _fallback_options(self, task: Any, advisory_context: dict[str, Any] | None = None) -> list[dict[str, str]]:
        advisory_context = advisory_context or {}
        task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
        local = advisory_context.get("local_llm") if isinstance(advisory_context.get("local_llm"), dict) else {}
        preferred_local = str((local or {}).get("recommended_model") or self._default_local_model(str(task_type).lower())).strip()
        options = [{"provider": self._default_local_provider(str(task_type).lower()), "model_name": preferred_local}]
        if str(task_type).lower() in {"plan", "review", "research"}:
            options.append({"provider": "openai", "model_name": "gpt-5.5"})
        return options

    def _safe_fallback_recommendation(self, task: Any, advisory_context: dict[str, Any] | None = None) -> MimoRecommendation:
        advisory_context = advisory_context or {}
        task_type = str(getattr(getattr(task, "type", None), "value", None) or getattr(task, "type", "unknown")).lower()
        complexity = str(getattr(getattr(task, "complexity", None), "value", None) or getattr(task, "complexity", "medium")).lower()
        local = advisory_context.get("local_llm") if isinstance(advisory_context.get("local_llm"), dict) else {}
        high_risk_tokens = {"security", "auth", "rbac", "payment", "production", "migration", "destructive"}
        task_text = self._task_text(task)
        is_high_risk = complexity in {"high", "critical"} or any(token in task_text for token in high_risk_tokens)
        if is_high_risk:
            model_name = self._default_local_model(task_type)
            provider_name = self._default_local_provider(task_type)
            return MimoRecommendation(
                provider=provider_name,
                model_name=model_name,
                confidence=0.1,
                allow=False,
                reason="mimo_unavailable_requires_escalation",
                selection_trace=[self._unavailable_decision_event(mode="safe_fallback", allow=False, reason="mimo_unavailable_requires_escalation", provider=provider_name, model_name=model_name)],
                requires_escalation=True,
                escalation_reason="mimo_unavailable_high_risk",
                blocked_by="policy",
                fallback_options=self._fallback_options(task, advisory_context),
                decision_mode="safe_fallback",
            )
        if bool((local or {}).get("ready")) and str((local or {}).get("recommended_owner") or "").strip().lower() == "local_llm":
            surrogate_model = str((local or {}).get("recommended_model") or (local or {}).get("preferred_model") or self._default_local_model(task_type)).strip()
            return MimoRecommendation(
                provider="local",
                model_name=surrogate_model,
                confidence=0.72,
                allow=True,
                reason="local_llm_surrogate_controller",
                selection_trace=[self._unavailable_decision_event(mode="surrogate_controller", allow=True, reason="local_llm_surrogate_controller", provider="local", model_name=surrogate_model)],
                fallback_options=self._fallback_options(task, advisory_context),
                decision_mode="surrogate_controller",
            )
        model_name = self._default_local_model(task_type)
        provider_name = self._default_local_provider(task_type)
        return MimoRecommendation(
            provider=provider_name,
            model_name=model_name,
            confidence=0.45,
            allow=True,
            reason="mimo_unavailable_safe_fallback",
            selection_trace=[self._unavailable_decision_event(mode="safe_fallback", allow=True, reason="mimo_unavailable_safe_fallback", provider=provider_name, model_name=model_name)],
            fallback_options=self._fallback_options(task, advisory_context),
            decision_mode="safe_fallback",
        )

    def recommend_model(self, task: Any, advisory_context: dict[str, Any] | None = None, *, current_budget: float, memory_context: dict[str, Any] | None = None) -> MimoRecommendation:
        advisory_context = advisory_context or {}
        requested_model = str(advisory_context.get("selected_model") or getattr(task, "assigned_model", "") or "unknown")
        context = self.build_selection_context(requested_model, task, current_budget, memory_context=memory_context)
        provider_health = self._provider_health(advisory_context)
        context["provider_health"] = provider_health
        context["decision_mode"] = "mimo_control" if self.is_available else "safe_fallback"
        if not self.is_available:
            return self._safe_fallback_recommendation(task, advisory_context)

        task_type = str(context.get("task_type") or "unknown").lower()
        inventory_candidates = self.resolve_candidate_models(task, advisory_context)
        normalized_provider = self._normalize_provider_name(str(advisory_context.get("selected_provider") or context.get("selected_provider") or (inventory_candidates[0].get("provider") if inventory_candidates else "local")))
        selected_model = str(advisory_context.get("selected_model") or context.get("selected_model") or requested_model or (inventory_candidates[0].get("model_name") if inventory_candidates else self._default_local_model(task_type))).strip()
        local = advisory_context.get("local_llm") if isinstance(advisory_context.get("local_llm"), dict) else {}
        complexity = str(getattr(getattr(task, "complexity", None), "value", None) or context.get("task_complexity") or "medium").lower()
        trace = list(context.get("selection_trace") or [])
        trace.append({"event": "provider_health", "providers": sorted(provider_health)})

        if not bool(context.get("context_window_ok", True)):
            return MimoRecommendation(
                provider=normalized_provider,
                model_name=selected_model,
                confidence=0.2,
                allow=False,
                reason="mimo_recommendation_blocked_context_limit",
                selection_trace=trace,
                requires_escalation=True,
                escalation_reason="context_limit_exceeded",
                blocked_by="context_limit",
                fallback_options=self._fallback_options(task, advisory_context),
            )

        health = provider_health.get(normalized_provider)
        if isinstance(health, dict) and health and not bool(health.get("ready", False)):
            return MimoRecommendation(
                provider=normalized_provider,
                model_name=selected_model,
                confidence=0.2,
                allow=False,
                reason="mimo_recommendation_blocked_provider_health",
                selection_trace=trace,
                requires_escalation=True,
                escalation_reason=str(health.get("error") or health.get("status") or "provider_unhealthy"),
                blocked_by="health",
                fallback_options=self._fallback_options(task, advisory_context),
            )

        mistral_governance = context.get("mistral_governance") if isinstance(context.get("mistral_governance"), dict) else {}
        if mistral_governance.get("selected_owner") == "mistral_gateway":
            model_name = str(mistral_governance.get("preferred_model") or "mistral-large-latest").strip()
            trace.append({"event": "mistral_gateway", "provider": "mistral", "model_name": model_name, "delegation_plan": len(mistral_governance.get("delegation_plan") or [])})
            return MimoRecommendation(
                provider="mistral",
                model_name=model_name,
                confidence=0.78,
                allow=True,
                reason=f"mistral_gateway_manager_{task_type}",
                selection_trace=trace,
                fallback_options=self._fallback_options(task, advisory_context),
            )

        if str((local or {}).get("recommended_owner") or "").strip().lower() == "local_llm" and task_type in {"plan", "docs", "research", "review"} and complexity in {"low", "medium"}:
            model_name = str((local or {}).get("recommended_model") or (local or {}).get("preferred_model") or self._default_local_model(task_type)).strip()
            trace.append({"event": "mimo_recommendation", "owner": "local_llm", "provider": "local", "model_name": model_name})
            return MimoRecommendation(
                provider="local",
                model_name=model_name,
                confidence=0.74,
                allow=True,
                reason=f"mimo_recommend_local_llm_owner_{task_type}",
                selection_trace=trace,
                fallback_options=self._fallback_options(task, advisory_context),
            )

        preferred_candidate = inventory_candidates[0] if inventory_candidates else None
        preferred_provider = str((preferred_candidate or {}).get("provider") or normalized_provider).strip()
        preferred_model = str(context.get("preferred_model") or selected_model or (preferred_candidate or {}).get("model_name") or self._default_local_model(task_type)).strip()
        preferred_value = dict((preferred_candidate or {}).get("value_diagnostics") or {})
        trace.append({"event": "mimo_recommendation", "provider": preferred_provider, "model_name": preferred_model, "inventory_candidates": len(inventory_candidates), "value_score": float((preferred_candidate or {}).get("value_score") or 0.0), "value_diagnostics": preferred_value})
        return MimoRecommendation(
            provider=preferred_provider,
            model_name=preferred_model,
            confidence=max(0.35, float((context.get("rolling_kpi") or {}).get("success_rate") or 0.5)),
            allow=True,
            reason="mimo_recommend_profile_selected",
            selection_trace=trace,
            fallback_options=self._fallback_options(task, advisory_context),
        )

    def safe_sync(self) -> None:
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                logger.debug("MIMO sync skipped in running event loop")
                return
            models = asyncio.run(self.bridge.refresh_cache())
            if models:
                for model in models:
                    self.state.set_context_limit(model.id or model.full_id, int(model.context_window or 0))
            self.is_available = self.bridge.is_cli_alive
            if self.is_available:
                self.last_failure_reason = None
                self.recovery_attempts = 0
                self.last_sync_at = Path(__file__).stat().st_mtime_ns and __import__('datetime').datetime.now(__import__('datetime').UTC).isoformat()
        except Exception as exc:
            self.is_available = False
            self.last_failure_reason = str(exc)
            self.recovery_attempts += 1
            self.last_sync_at = __import__('datetime').datetime.now(__import__('datetime').UTC).isoformat()
            logger.warning("MIMO director sync failed: %s", exc)

    def _runtime_health(self) -> dict[str, Any]:
        snapshot = build_mimo_runtime_status(
            bridge=self.bridge,
            status_source_configured=self._status_source is not None,
            failure_reason=self.last_failure_reason,
            recovery_attempts=self.recovery_attempts,
            last_sync_at=self.last_sync_at,
            profiles_loaded=len(self.task_profiles),
        )
        return {
            "ready": bool(self.is_available),
            "profiles_loaded": len(self.task_profiles),
            "bridge_cli_alive": bool(getattr(self.bridge, "is_cli_alive", False)),
            "profile_manifest_present": self.profile_manifest_path.exists(),
            "status_source_configured": self._status_source is not None,
            "failure_reason": self.last_failure_reason,
            "recovery_attempts": self.recovery_attempts,
            "last_sync_at": self.last_sync_at,
            "report_present": bool(snapshot.get("report_present")),
            "usable_count": int(snapshot.get("usable_count") or 0),
            "failed_count": int(snapshot.get("failed_count") or 0),
            "auth_categories": snapshot.get("auth_categories", {}),
        }

    def build_selection_context(self, model_name: str, task: Any, current_budget: float, memory_context: dict[str, Any] | None = None) -> dict[str, Any]:
        task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
        self.reload_profiles_if_changed()
        context: dict[str, Any] = {
            "mimo_available": self.is_available,
            "requested_model": model_name,
            "budget_remaining": self._remaining_budget_for_model(model_name, current_budget),
            "task_complexity": getattr(getattr(task, "complexity", None), "value", None) or "medium",
            "task_scope": getattr(task, "memory_scope", "task"),
            "task_session_id": getattr(task, "session_id", None),
            "task_type": task_type,
            "kpi_threshold": getattr(self._kpi_source, "threshold", None),
            "quality_min_confidence": getattr(self._quality_source, "minimum_confidence", None),
        }
        scope = context["task_scope"]
        identifier = getattr(task, "session_id", None) or getattr(task, "task_id", None) or "default"
        scoped_budget = self.state.get_scoped_budget(str(scope), str(identifier))
        if scoped_budget is not None:
            context["scoped_budget_remaining"] = scoped_budget.remaining_tokens
            context["scoped_budget_balance"] = scoped_budget.balance

        memory_context = memory_context or {}
        context_bytes = len(str(memory_context).encode("utf-8"))
        context["memory_context_bytes"] = context_bytes
        context["context_window_ok"] = self.state.validate_context_limit(model_name, context_bytes)
        memory_diag = self._memory_source.diagnostic_snapshot() if self._memory_source is not None and hasattr(self._memory_source, "diagnostic_snapshot") else {}
        context["memory_diagnostics"] = memory_diag if isinstance(memory_diag, dict) else {}
        if self._memory_source is not None and hasattr(self._memory_source, "list_keys"):
            try:
                context["memory_keys_count"] = len(self._memory_source.list_keys())
            except Exception:
                context["memory_keys_count"] = None
        if self._budget_module is not None and hasattr(self._budget_module, "evaluate_model_budget"):
            try:
                context["model_budget"] = self._budget_module.evaluate_model_budget(model_name, planned_tokens=context_bytes // 4)
            except Exception as exc:
                context["model_budget_error"] = str(exc)
        context.update(self._historical_quality_context(task, model_name))
        context.update(self._task_budget_pressure(task, context))
        context["vfs_pressure"] = self._vfs_pressure(task, context)
        context["selected_provider"] = str(context.get("selected_provider") or context.get("provider") or "local")
        context["selected_model"] = model_name
        context["provider_health"] = self._provider_health(memory_context if isinstance(memory_context, dict) else None)
        context["decision_mode"] = "mimo_control" if self.is_available else "safe_fallback"
        context["memory_efficiency"] = memory_efficiency_score(memory_context_bytes=context_bytes, context_window=self.state.model_context_limits.get(model_name), memory_keys_count=context.get("memory_keys_count"), hot_count=context.get("memory_diagnostics", {}).get("hot_count"), hot_capacity=context.get("memory_diagnostics", {}).get("hot_capacity"), persistent_enabled=context.get("memory_diagnostics", {}).get("persistent_enabled"))
        context["mimo_runtime_health"] = self._runtime_health()
        mistral_health = context["provider_health"].get("mistral") if isinstance(context.get("provider_health"), dict) else {}
        context["mistral_governance"] = self.mistral_governance.build_profile(
            task,
            local_advisory=memory_context if isinstance(memory_context, dict) else {},
            current_budget=current_budget,
            provider_ready=bool((mistral_health or {}).get("ready", bool(context.get("mimo_available")))),
        )
        task_profile = self._task_profile(task_type, task=task, context=context)
        context["task_profile"] = task_profile
        context["profile_weights"] = self._profile_weights(str(task_profile.get("task_type") or context["task_type"]), str(context.get("selected_provider") or "local"), model_name, task=task, context=context, profile=task_profile)
        context["rolling_kpi"] = self._rolling_kpi(task, model_name)
        context["context_depth"] = self._context_depth_for(task, context, model_name, profile=task_profile)
        context["selection_trace"] = [
            {
                "event": "runtime_health",
                "ready": context["mimo_runtime_health"]["ready"],
                "profiles_loaded": context["mimo_runtime_health"]["profiles_loaded"],
            },
            {
                "event": "profile_selected",
                "task_type": task_type,
                "profile_key": str(task_profile.get("profile_key") or task_profile.get("task_type") or task_type),
                "requested_model": model_name,
                "preferred_model": str(context.get("preferred_model") or model_name),
            },
            {
                "event": "mistral_governance",
                "selected_owner": str((context.get("mistral_governance") or {}).get("selected_owner") or ""),
                "preferred_model": str((context.get("mistral_governance") or {}).get("preferred_model") or ""),
                "management_profile": str((context.get("mistral_governance") or {}).get("management_profile") or ""),
            },
        ]
        return context

    def validate_and_correct(self, model: Any, task: Any, current_budget: float, memory_context: dict[str, Any] | None = None) -> Any:
        if not self.is_available:
            return model
        model_name = getattr(model, "model_name", None) or getattr(model, "name", None) or str(model)
        task_complexity = getattr(getattr(task, "complexity", None), "value", None) or "medium"
        budget_remaining = self._remaining_budget_for_model(model_name, current_budget)
        if memory_context is not None:
            memory_bytes = len(str(memory_context).encode("utf-8"))
            if not self.state.validate_context_limit(model_name, memory_bytes):
                model_name = self.state.default_fallback_model
                budget_remaining = current_budget
        allowed = self.state.get_allowed_model(model_name, task_complexity, budget_remaining)
        if allowed == model_name:
            return model
        if hasattr(model, "model_name"):
            model.model_name = allowed
        elif hasattr(model, "name"):
            model.name = allowed
        logger.info("MIMO director corrected model %s -> %s", model_name, allowed)
        return model

    def _historical_quality_context(self, task: Any, model_name: str) -> dict[str, Any]:
        if self._history_source is None:
            return {}
        session_id = getattr(task, "session_id", None) or getattr(task, "task_id", None) or "default"
        task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
        summary: dict[str, Any] = {}
        try:
            history_backend = getattr(self._history_source, "hybrid", None)
            persistent = getattr(history_backend, "persistent", None)
            if persistent is not None:
                commands = persistent.list_recent_commands_by_session(session_id=session_id, limit=8)
                memories = persistent.retrieve_memories(session_id=session_id, agent_id=model_name, memory_type="episodic", top_k=8)
                summary["history_commands_count"] = len(commands)
                summary["history_memories_count"] = len(memories)
                if commands:
                    successes = sum(1 for item in commands if item.get("success"))
                    avg_tokens = sum(int(item.get("tokens_used") or 0) for item in commands) / max(1, len(commands))
                    summary["history_command_success_rate"] = round(successes / len(commands), 3)
                    summary["history_avg_tokens"] = round(avg_tokens, 2)
                if memories:
                    importance = sum(float(item.importance_score) for item in memories) / max(1, len(memories))
                    summary["history_importance_avg"] = round(importance, 3)
        except Exception as exc:
            summary["history_error"] = str(exc)
        if self._kpi_source is not None and hasattr(self._kpi_source, "threshold"):
            summary["kpi_threshold"] = getattr(self._kpi_source, "threshold", None)
        if self._quality_source is not None and hasattr(self._quality_source, "minimum_confidence"):
            summary["quality_min_confidence"] = getattr(self._quality_source, "minimum_confidence", None)
        summary["task_type"] = task_type
        summary["task_profile"] = self._task_profile(task_type)
        summary["preferred_model"] = self._select_historical_model(task_type, summary, model_name)
        return summary

    def _select_historical_model(self, task_type: str, summary: dict[str, Any], requested_model: str) -> str:
        score = float(summary.get("history_command_success_rate") or 0.0)
        avg_tokens = float(summary.get("history_avg_tokens") or 0.0)
        if task_type in {"plan", "research"} and score >= 0.75:
            return requested_model
        if task_type == "review" and score < 0.65:
            return "gpt-5.4-mini"
        if task_type in {"code", "test"} and avg_tokens > 1200:
            return "qwen2.5:32b-instruct-q4_k_m"
        if task_type == "docs" and score >= 0.8:
            return "qwen-2.5-7b-instruct"
        return requested_model

    def _task_budget_pressure(self, task: Any, context: dict[str, Any]) -> dict[str, Any]:
        task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
        remaining = float(context.get("budget_remaining") or 0.0)
        profile = self._profile(task_type, task=task, context=context)
        thresholds = profile.get("budget_pressure") or {}
        pressure = "normal"
        if remaining < float(thresholds.get("high", 0.0) or 0.0):
            pressure = "high"
        elif remaining < float(thresholds.get("medium", 0.0) or 0.0):
            pressure = "medium"
        return {"budget_pressure": pressure}

    def _vfs_pressure(self, task: Any, context: dict[str, Any]) -> str:
        if self._vfs_source is None:
            return "normal"
        try:
            node_count = 0
            if hasattr(self._vfs_source, "finalize"):
                summary = self._vfs_source.finalize()
                node_count = int(summary.get("node_count") or 0)
            elif hasattr(self._vfs_source, "_nodes"):
                node_count = len(getattr(self._vfs_source, "_nodes", {}))
            task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
            profile = self._profile(task_type, task=task, context=context)
            weights = self._profile_weights(task_type, str(context.get("selected_provider") or "local"), getattr(task, "assigned_model", ""))
            weighted = node_count * float(weights.get("vfs", 1.0))
            if weighted > 250:
                return "high"
            if weighted > 120:
                return "medium"
            if weighted > 60:
                return "low"
        except Exception:
            return "normal"
        return "normal"

    def _task_profile(self, task_type: str, task: Any | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._profile(task_type, task=task, context=context)

    def register_execution_result(self, model_name: str, is_successful: bool, latency: float, input_tokens: int = 0, output_tokens: int = 0, task: Any | None = None, quality_score: float = 0.0, provider: str = "local") -> None:
        score = self.state.update_score(model_name, is_successful, latency)
        if input_tokens or output_tokens:
            self.state.deduct_tokens(model_name, input_tokens, output_tokens)
        if self._budget_module is not None and hasattr(self._budget_module, "stats"):
            stat = self._budget_module.stats.get(model_name)
            if stat is not None:
                stat.used_tokens += max(0, int(input_tokens + output_tokens))
        if task is not None:
            self._record_window(task, model_name, is_successful, latency, quality_score or score)
        self._persist_task_aggregate(task, model_name, is_successful, latency, input_tokens, output_tokens, score, quality_score=quality_score, provider=provider)
        logger.debug("MIMO execution registered model=%s score=%.3f success=%s", model_name, score, is_successful)

    def register_task_budget(self, scope: str, identifier: str, *, balance: float | None = None, limit_tokens: int | None = None) -> None:
        self.state.set_scoped_budget(scope, identifier, balance=balance, limit_tokens=limit_tokens)

    def consume_task_budget(self, scope: str, identifier: str, tokens: int) -> int:
        return self.state.deduct_scoped_tokens(scope, identifier, tokens)

    def _persist_task_aggregate(self, task: Any | None, model_name: str, is_successful: bool, latency: float, input_tokens: int, output_tokens: int, score: float, quality_score: float = 0.0, provider: str = "local") -> None:
        if task is None or self._history_source is None:
            return
        try:
            history_backend = getattr(self._history_source, "hybrid", None)
            persistent = getattr(history_backend, "persistent", None)
            if persistent is None or not hasattr(persistent, "store_memory"):
                return
            task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
            session_id = getattr(task, "session_id", None) or getattr(task, "task_id", None) or "default"
            window = self.task_kpi_windows.get((task_type, model_name))
            rolling = window.snapshot() if window else {"success_rate": 0.5, "avg_latency": float(latency), "avg_quality": float(quality_score or score)}
            content = {
                "task_type": task_type,
                "model": model_name,
                "success": bool(is_successful),
                "latency": float(latency),
                "tokens": int(input_tokens + output_tokens),
                "quality_score": float(quality_score or score),
                "rolling_kpi": rolling,
                "budget_pressure": self._task_budget_pressure(task, {"budget_remaining": self._remaining_budget_for_model(model_name, 0.0)})["budget_pressure"],
                "vfs_pressure": self._vfs_pressure(task, {}),
                "profile_weights": self._profile_weights(task_type, provider, model_name, task=task, context={"budget_pressure": self._task_budget_pressure(task, {"budget_remaining": self._remaining_budget_for_model(model_name, 0.0)})["budget_pressure"], "vfs_pressure": self._vfs_pressure(task, {}), "quality_min_confidence": quality_score}),
            }
            persistent.store_memory(session_id=session_id, agent_id=model_name, memory_type=f"kpi_task:{task_type}", content=content, metadata={"key": f"{task_type}:{session_id}"}, importance_score=min(1.0, max(0.1, score)))
        except Exception as exc:
            logger.debug("MIMO aggregate persistence skipped: %s", exc)

    def _aggregate_model_kpi(self, model_name: str) -> dict[str, float]:
        success_weight = 0.0
        latency_weight = 0.0
        quality_weight = 0.0
        sample_weight = 0.0
        for (task_type, model), window in self.task_kpi_windows.items():
            if model != model_name:
                continue
            snap = window.snapshot()
            samples = float(max(1, len(window.latencies)))
            success_weight += float(snap.get("success_rate") or 0.5) * samples
            latency_weight += float(snap.get("avg_latency") or 0.0) * samples
            quality_weight += float(snap.get("avg_quality") or 0.5) * samples
            sample_weight += samples
        if sample_weight <= 0:
            return {"success_rate": 0.5, "avg_latency": 0.0, "avg_quality": 0.5, "sample_size": 0.0}
        return {
            "success_rate": round(success_weight / sample_weight, 4),
            "avg_latency": round(latency_weight / sample_weight, 4),
            "avg_quality": round(quality_weight / sample_weight, 4),
            "sample_size": sample_weight,
        }

    def _report_inventory(self) -> list[Any]:
        inventory = list(self.bridge.get_cached_models()) if hasattr(self.bridge, "get_cached_models") else []
        if inventory:
            return inventory

        fallback_models: list[tuple[str, str, int | None, str, list[str]]] = [
            ("local", "qwen-2.5-7b-instruct", 131072, "low", ["docs", "research", "review"]),
            ("ai_kernel", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m", 65536, "medium", ["code", "review", "fix", "test"]),
            ("local", "qwen2.5:32b-instruct-q4_k_m", 65536, "medium", ["code", "review", "fix"]),
                        ("mistral", "mistral-medium-latest", 131072, "medium", ["docs", "research", "review"]),
            ("mistral", "mistral-large-latest", 131072, "high", ["code", "review", "research"]),
            ("mistral", "codestral-latest", 131072, "medium", ["code", "fix", "test"]),
            ("mistral", "devstral-latest", 131072, "medium", ["code", "fix", "review"]),
        ]

        try:
            openai_catalog = OpenAIModelRegistry().get_catalog()
            openai_models = openai_catalog.all_models or ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-5.5"]
        except Exception:
            openai_models = ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-5.5"]
        for model_name in openai_models:
            fallback_models.append(("openai", model_name, 128000, "medium", ["docs", "code", "review", "research"]))

        try:
            qwen_catalog = QwenModelRegistry().get_catalog()
            qwen_models = qwen_catalog.coder + qwen_catalog.instruct + qwen_catalog.max + qwen_catalog.plus + qwen_catalog.turbo + qwen_catalog.standard
        except Exception:
            qwen_models = ["qwen-2.5-coder-32b", "qwen-2.5-7b-instruct"]
        for model_name in qwen_models:
            fallback_models.append(("local", model_name, 131072, "low" if "7b" in model_name.lower() else "medium", ["docs", "code", "review"]))

        deduped: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for provider, model_name, context_window, cost_class, tags in fallback_models:
            key = (provider, model_name)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(MimoModelSnapshot(
                full_id=f"{provider}/{model_name}",
                id=model_name,
                provider=provider,
                status="synthetic",
                context_window=context_window,
                capability_tags=tags,
                cost_class=cost_class,
                ready=True,
                blocked=False,
            ))
        return deduped

    def model_value_report(self, advisory_context: dict[str, Any] | None = None) -> dict[str, Any]:
        advisory_context = advisory_context or {}
        memory_diag = self._memory_source.diagnostic_snapshot() if self._memory_source is not None and hasattr(self._memory_source, "diagnostic_snapshot") else {}
        memory_efficiency = float(memory_diag.get("memory_efficiency_score") or 1.0)
        rows: list[dict[str, Any]] = []
        inventory = self._report_inventory()
        for model in inventory:
            provider = self._normalize_provider_name(getattr(model, "provider", "local"))
            model_name = str(getattr(model, "id", "") or getattr(model, "full_id", "")).strip()
            aggregate = self._aggregate_model_kpi(model_name)
            observed_cost = self._observed_model_cost(model_name, provider)
            value = compute_model_value(
                success_rate=float(aggregate.get("success_rate") or 0.5),
                quality_score=float(aggregate.get("avg_quality") or 0.5),
                latency_ms=float(aggregate.get("avg_latency") or 0.0) * 1000.0,
                cost_usd=float(observed_cost.get("avg_cost_usd") or 0.0),
                memory_efficiency=memory_efficiency,
                availability=1.0 if bool(getattr(model, "ready", True)) else 0.25,
                specialization=1.0,
                context_fit=context_fit_score(getattr(model, "context_window", None)),
            )
            rows.append({
                "provider": provider,
                "model": model_name,
                "avg_cost_usd": float(observed_cost.get("avg_cost_usd") or 0.0),
                "cost_samples": int(observed_cost.get("samples") or 0),
                "latency_sec": float(aggregate.get("avg_latency") or 0.0),
                "quality": float(aggregate.get("avg_quality") or 0.5),
                "success_rate": float(aggregate.get("success_rate") or 0.5),
                "memory_efficiency": memory_efficiency,
                "value_score": float(value.get("value_score") or 0.0),
                "value_components": dict(value.get("components") or {}),
                "context_window": int(getattr(model, "context_window", 0) or 0),
                "cost_class": str(getattr(model, "cost_class", "") or ""),
            })
        rows.sort(key=lambda item: (-float(item.get("value_score") or 0.0), float(item.get("avg_cost_usd") or 0.0), float(item.get("latency_sec") or 0.0)))
        for index, row in enumerate(rows, start=1):
            row["rank"] = index
        return {"generated_at": __import__('datetime').datetime.now(__import__('datetime').UTC).isoformat(), "memory_efficiency": memory_efficiency, "models": rows}

    def _remaining_budget_for_model(self, model_name: str, fallback_budget: float) -> float:
        if self._budget_module is None:
            return fallback_budget
        try:
            stat = self._budget_module.stats.get(model_name)
            if stat is None:
                return fallback_budget
            return float(stat.remaining_tokens)
        except Exception:
            return fallback_budget
