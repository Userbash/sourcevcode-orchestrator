from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from .data_storage_analytics import build_data_storage_analytics_report, write_data_storage_analytics_report
from .kernel_api import KernelAPI
from .models import Task


class DataAnalyticsModule:
    name: str = "data_analytics"
    _runtime_agent_id: str = "system:data_analytics"

    def __init__(self) -> None:
        self._api: KernelAPI | None = None
        self._is_active = False
        self._lock = threading.Lock()
        self._last_refresh_monotonic = 0.0
        self._last_refresh_at: str | None = None
        self._last_error: str | None = None
        self._last_report: dict[str, Any] = {}

    async def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._is_active = True
        self.refresh(force=True, reason="module_load")

    async def on_unload(self) -> None:
        self._is_active = False

    @staticmethod
    def _report_signals(report: dict[str, Any]) -> dict[str, Any]:
        signals = report.get("operational_signals", {})
        return signals if isinstance(signals, dict) else {}

    @classmethod
    def _report_policy(cls, report: dict[str, Any]) -> dict[str, Any]:
        policy = cls._report_signals(report).get("routing_policy", {})
        return policy if isinstance(policy, dict) else {}

    @classmethod
    def _task_analytics_payload(cls, report: dict[str, Any]) -> dict[str, Any]:
        signals = cls._report_signals(report)
        audit = report.get("audit", {}) if isinstance(report.get("audit"), dict) else {}
        return {
            "freshness_status": signals.get("freshness_status"),
            "retention_status": signals.get("retention_status"),
            "session_coverage_status": signals.get("session_coverage_status"),
            "search_readiness": signals.get("search_readiness"),
            "retrieval_readiness": signals.get("retrieval_readiness"),
            "orchestrator_confidence": signals.get("orchestrator_confidence"),
            "analytics_ready": bool(signals.get("analytics_ready")),
            "analytics_signal_score": signals.get("analytics_signal_score"),
            "issues": list(audit.get("issues") or []),
            "routing_policy": cls._report_policy(report),
            "source": report.get("source"),
            "generated_at": report.get("generated_at"),
        }

    @classmethod
    def _runtime_payload(cls, report: dict[str, Any], *, reason: str, report_path: Path) -> dict[str, Any]:
        task_payload = cls._task_analytics_payload(report)
        policy = task_payload.get("routing_policy") if isinstance(task_payload.get("routing_policy"), dict) else {}
        return {
            "status": task_payload.get("orchestrator_confidence") or "unknown",
            "source": "data_analytics_module",
            "reason": reason,
            "analytics_ready": task_payload.get("analytics_ready"),
            "freshness_status": task_payload.get("freshness_status"),
            "search_readiness": task_payload.get("search_readiness"),
            "retrieval_readiness": task_payload.get("retrieval_readiness"),
            "analytics_signal_score": task_payload.get("analytics_signal_score"),
            "policy_mode": policy.get("policy_mode"),
            "memory_retrieval_mode": policy.get("memory_retrieval_mode"),
            "degraded_reasons": list(policy.get("degraded_reasons") or []),
            "report_path": str(report_path),
            "generated_at": report.get("generated_at"),
        }

    def _publish_runtime_state(self, payload: dict[str, Any]) -> None:
        if not self._api:
            return
        hub = getattr(self._api, "runtime_event_stream_hub", None)
        publish = getattr(hub, "publish_agent_event", None)
        if callable(publish):
            publish(self._runtime_agent_id, payload)

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        report = self._last_report if isinstance(self._last_report, dict) else {}
        if not report:
            return
        if not isinstance(task.routing_hints, dict):
            task.routing_hints = {}
        payload = self._task_analytics_payload(report)
        policy = payload.get("routing_policy") if isinstance(payload.get("routing_policy"), dict) else {}
        task.routing_hints["data_analytics"] = payload
        if policy:
            task.routing_hints.setdefault("memory_retrieval_mode", policy.get("memory_retrieval_mode"))
            task.routing_hints.setdefault("prefer_fresh_context", bool(policy.get("prefer_fresh_context")))
            task.routing_hints.setdefault("data_analytics_policy_mode", policy.get("policy_mode"))
            task.routing_hints.setdefault("search_enabled", bool(policy.get("search_enabled")))
            task.routing_hints.setdefault("retrieval_enabled", bool(policy.get("retrieval_enabled")))
        context["data_analytics"] = payload
        context["data_analytics_policy"] = dict(policy)

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        self.refresh(reason="after_task")

    def _refresh_interval_sec(self) -> int:
        raw = (os.getenv("AI_BRIDGE_DATA_ANALYTICS_REFRESH_SEC", "300") or "300").strip()
        try:
            return max(30, int(raw))
        except ValueError:
            return 300

    def _report_path(self, storage_root: str) -> Path:
        configured = (os.getenv("AI_BRIDGE_DATA_ANALYTICS_REPORT_PATH", "") or "").strip()
        if configured:
            return Path(configured)
        return Path(storage_root) / "data_storage_analytics.json"

    def refresh(self, *, force: bool = False, reason: str = "manual") -> bool:
        if not self._is_active:
            return False
        now = time.monotonic()
        with self._lock:
            if not force and self._last_refresh_monotonic and now - self._last_refresh_monotonic < self._refresh_interval_sec():
                return False
            try:
                report = build_data_storage_analytics_report()
                report_path = self._report_path(report.storage_root)
                write_data_storage_analytics_report(report, output_path=report_path)
                self._last_report = report.as_dict()
                self._last_refresh_at = report.generated_at
                self._last_refresh_monotonic = now
                self._last_error = None
                runtime_payload = self._runtime_payload(self._last_report, reason=reason, report_path=report_path)
                if self._api:
                    self._api.emit_event(
                        "DATA_ANALYTICS",
                        {
                            "reason": reason,
                            "status": report.operational_signals.get("orchestrator_confidence"),
                            "freshness": report.operational_signals.get("freshness_status"),
                            "retrieval": report.operational_signals.get("retrieval_readiness"),
                            "signal_score": report.operational_signals.get("analytics_signal_score"),
                            "policy_mode": runtime_payload.get("policy_mode"),
                        },
                    )
                self._publish_runtime_state(runtime_payload)
                return True
            except Exception as exc:
                self._last_error = str(exc)
                self._publish_runtime_state({
                    "status": "error",
                    "source": "data_analytics_module",
                    "reason": reason,
                    "last_error": str(exc),
                    "analytics_ready": False,
                })
                if self._api:
                    self._api.log("error", f"[DATA_ANALYTICS] refresh failed: {exc}")
                return False

    def finalize(self) -> dict[str, Any]:
        report = self._last_report if isinstance(self._last_report, dict) else {}
        management = report.get("management_summary", {}) if isinstance(report.get("management_summary"), dict) else {}
        audit = report.get("audit", {}) if isinstance(report.get("audit"), dict) else {}
        signals = report.get("operational_signals", {}) if isinstance(report.get("operational_signals"), dict) else {}
        return {
            "status": "active" if self._is_active else "inactive",
            "last_refresh_at": self._last_refresh_at,
            "refresh_interval_sec": self._refresh_interval_sec(),
            "last_error": self._last_error,
            "audit": audit,
            "operational_signals": signals,
            "management_summary": management,
            "analytics_dimensions": report.get("analytics_dimensions", []),
            "recommendations": report.get("recommendations", []),
            "priorities": report.get("priorities", []),
            "storage_root": report.get("storage_root"),
            "source": report.get("source"),
            "report_path": str(self._report_path(str(report.get("storage_root") or "memory_store"))),
        }
