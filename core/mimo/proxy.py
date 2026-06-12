from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any

from .bridge import MimoAsyncBridge
from .state import MimoStateContext

logger = logging.getLogger(__name__)


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


class MimoOrchestrationDirector:
    def __init__(self) -> None:
        self.state = MimoStateContext()
        self.bridge = MimoAsyncBridge()
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
        self._load_persisted_kpi_windows()

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
            if any(token in task_text for token in ("regression", "flaky", "failure", "broken", "rerun")):
                candidates.append("test_regression")
            if task_priority in {"high", "critical"}:
                candidates.append("test_critical")
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

    def _profile_weights(self, task_type: str, provider: str, model_name: str, task: Any | None = None, context: dict[str, Any] | None = None) -> dict[str, float]:
        profile = self._profile(task_type, task=task, context=context)
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
        for token, key in (("qwen", "qwen"), ("deepseek", "deepseek"), ("gpt", "gpt")):
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

    def _context_depth_for(self, task: Any, context: dict[str, Any], model_name: str) -> int:
        task_type = getattr(getattr(task, "type", None), "value", None) or str(getattr(task, "type", "unknown"))
        profile = self._profile_weights(task_type, str(context.get("selected_provider") or "local"), model_name, task=task, context=context)
        budget_pressure = str(context.get("budget_pressure") or "normal")
        vfs_pressure = str(context.get("vfs_pressure") or "normal")
        quality_min = float(context.get("quality_min_confidence") or 0.0)
        task_priority = str(getattr(getattr(task, "priority", None), "value", None) or getattr(task, "priority", "normal")).lower()
        rolling = self._rolling_kpi(task, model_name)
        depth = int(self._profile(task_type).get("default_context_depth") or 1)
        depth += 1 if profile.get("quality", 1.0) > 1.1 else 0
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
        manifest_entries: list[str] = []
        if self.profile_manifest_path.exists():
            try:
                stat = self.profile_manifest_path.stat()
                self._profile_mtimes[self.profile_manifest_path] = stat.st_mtime
                manifest = json.loads(self.profile_manifest_path.read_text(encoding="utf-8"))
                if isinstance(manifest, dict):
                    for section in ("task_profiles", "provider_profiles", "model_profiles", "combo_profiles"):
                        entries = manifest.get(section)
                        if isinstance(entries, list):
                            manifest_entries.extend(str(item) for item in entries)
            except Exception:
                manifest_entries = []
        files = [self.profile_dir / entry for entry in manifest_entries if (self.profile_dir / entry).is_file()]
        if not files:
            files = [file for file in self.profile_dir.rglob("*.json") if file.name != "rolling_kpi_store.json" and file.name != "manifest.json"]
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
        manifest_entries: list[Path] = []
        if self.profile_manifest_path.exists():
            try:
                stat = self.profile_manifest_path.stat()
                current_mtimes[self.profile_manifest_path] = stat.st_mtime
                if self._profile_mtimes.get(self.profile_manifest_path) != stat.st_mtime:
                    changed = True
                manifest = json.loads(self.profile_manifest_path.read_text(encoding="utf-8"))
                if isinstance(manifest, dict):
                    for section in ("task_profiles", "provider_profiles", "model_profiles", "combo_profiles"):
                        entries = manifest.get(section)
                        if isinstance(entries, list):
                            manifest_entries.extend(self.profile_dir / str(item) for item in entries)
            except Exception:
                manifest_entries = []
        files = manifest_entries or [file for file in self.profile_dir.rglob("*.json") if file.name not in {"rolling_kpi_store.json", "manifest.json"}]
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
        except Exception as exc:
            self.is_available = False
            logger.warning("MIMO director sync failed: %s", exc)

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
        context["task_profile"] = self._task_profile(task_type, task=task, context=context)
        context["profile_weights"] = self._profile_weights(context["task_type"], str(context.get("selected_provider") or "local"), model_name, task=task, context=context)
        context["rolling_kpi"] = self._rolling_kpi(task, model_name)
        context["context_depth"] = self._context_depth_for(task, context, model_name)
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
            return "gpt-4o"
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
