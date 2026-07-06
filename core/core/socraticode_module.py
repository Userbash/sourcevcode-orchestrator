from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kernel_protocol import KernelAPI, KernelModule

_SUPPORTED_TASK_TYPES = {"code", "review", "test", "plan"}
_BRIDGE_CONTEXT_KEYS = (
    "socraticode_bridge",
    "bridge:socraticode",
    "bridge_socraticode",
)
_BRIDGE_MODULE_KEYS = (
    "socraticode_bridge",
    "socraticode",
)


@dataclass(slots=True)
class SocratiCodeModule(KernelModule):
    name: str = "socraticode"
    _api: KernelAPI | None = None
    _bridge_source: str | None = None
    _bridge_available: bool = False
    _annotations_total: int = 0
    _skipped_total: int = 0
    _failures_total: int = 0
    _last_error: str | None = None
    _last_annotation: dict[str, Any] = field(default_factory=dict)

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        bridge, source = self._resolve_bridge()
        self._bridge_available = bridge is not None
        self._bridge_source = source
        api.log(
            "info",
            f"[SOCRATICODE] loaded bridge_available={self._bridge_available} source={self._bridge_source or 'none'}",
        )

    def on_unload(self) -> None:
        self._api = None

    @staticmethod
    def _task_type(task: Any) -> str:
        return str(getattr(getattr(task, "type", None), "value", getattr(task, "type", "")) or "").strip().lower()

    @staticmethod
    def _description(task: Any) -> str:
        task_input = getattr(task, "input", None)
        return str(getattr(task_input, "description", "") or "").strip()

    @staticmethod
    def _ensure_routing_hints(task: Any) -> dict[str, Any]:
        routing_hints = getattr(task, "routing_hints", None)
        if isinstance(routing_hints, dict):
            return routing_hints
        routing_hints = {}
        setattr(task, "routing_hints", routing_hints)
        return routing_hints

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return round(parsed, 4)

    @staticmethod
    def _safe_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value or "").strip().lower() in {"1", "true", "yes", "on", "eligible"}

    def _resolve_bridge(self) -> tuple[Any | None, str | None]:
        if self._api is None:
            return None, None

        for key in _BRIDGE_CONTEXT_KEYS:
            try:
                bridge = self._api.get_context(key)
            except Exception:
                continue
            if bridge is not None:
                return bridge, f"context:{key}"

        for key in _BRIDGE_MODULE_KEYS:
            try:
                bridge = self._api.get_module(key)
            except Exception:
                continue
            if bridge is not None:
                return bridge, f"module:{key}"

        return None, None

    @staticmethod
    def _invoke_bridge_method(bridge: Any, method_name: str, task: Any, context: dict[str, Any]) -> Any:
        method = getattr(bridge, method_name, None)
        if not callable(method):
            return None

        attempts = (
            (),
            (task, context),
            (task,),
        )
        keyword_attempts = (
            {"task": task, "context": context},
            {
                "task": task,
                "context": context,
                "description": SocratiCodeModule._description(task),
                "task_type": SocratiCodeModule._task_type(task),
                "routing_hints": getattr(task, "routing_hints", {}),
            },
            {
                "description": SocratiCodeModule._description(task),
                "task_type": SocratiCodeModule._task_type(task),
                "routing_hints": getattr(task, "routing_hints", {}),
            },
        )

        for kwargs in keyword_attempts:
            try:
                return method(**kwargs)
            except TypeError:
                continue

        for args in attempts:
            try:
                return method(*args)
            except TypeError:
                continue

        return method()

    def _invoke_bridge(self, bridge: Any, task: Any, context: dict[str, Any]) -> dict[str, Any]:
        for method_name in ("analyze_task", "analyze", "assess_task", "assess", "recommend", "build_advisory"):
            response = self._invoke_bridge_method(bridge, method_name, task, context)
            if isinstance(response, dict):
                return response
        return {}

    def _normalize_context_coverage(self, payload: dict[str, Any], task: Any) -> dict[str, Any]:
        section = payload.get("context_coverage")
        if not isinstance(section, dict):
            section = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}

        task_input = getattr(task, "input", None)
        requested_files = [str(item).strip() for item in list(getattr(task_input, "files", []) or []) if str(item).strip()]
        covered_files = [str(item).strip() for item in list(section.get("covered_files", [])) if str(item).strip()]
        missing_files = [str(item).strip() for item in list(section.get("missing_files", [])) if str(item).strip()]

        if requested_files and not covered_files and not missing_files:
            missing_files = list(requested_files)

        score = self._safe_float(section.get("score"))
        if score is None:
            ratio = section.get("coverage_ratio")
            score = self._safe_float(ratio)
        if score is None and requested_files:
            score = round(len(covered_files) / len(requested_files), 4)
        if score is None:
            score = 0.0

        return {
            "score": score,
            "coverage_ratio": score,
            "covered_files": covered_files,
            "missing_files": missing_files,
            "summary": str(section.get("summary") or section.get("note") or "").strip(),
        }

    def _normalize_cost_downgrade(self, payload: dict[str, Any]) -> dict[str, Any]:
        section = payload.get("cost_downgrade")
        if not isinstance(section, dict):
            section = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}

        eligible = self._safe_bool(section.get("eligible"))
        if not eligible and self._safe_float(section.get("confidence")) not in {None, 0.0}:
            eligible = str(section.get("recommendation") or "").strip().lower() == "downgrade"

        return {
            "eligible": eligible,
            "target_cost_tier": str(section.get("target_cost_tier") or section.get("cost_tier") or "economy").strip(),
            "preferred_provider": str(section.get("preferred_provider") or "").strip() or None,
            "reason": str(section.get("reason") or section.get("summary") or "").strip(),
            "confidence": self._safe_float(section.get("confidence")) or 0.0,
        }

    def _normalize_parallelism(self, payload: dict[str, Any], routing_hints: dict[str, Any]) -> dict[str, Any]:
        section = payload.get("parallelism")
        if not isinstance(section, dict):
            section = payload.get("parallel") if isinstance(payload.get("parallel"), dict) else {}

        current = self._safe_int(routing_hints.get("parallel_branches"))
        if current is None:
            current = self._safe_int(section.get("current_parallel_branches"))

        recommended = self._safe_int(
            section.get("recommended_parallel_branches")
            or section.get("target_parallel_branches")
            or section.get("max_parallel_branches")
        )
        reduction_by = self._safe_int(section.get("reduce_by"))
        if recommended is None and current is not None and reduction_by is not None and reduction_by <= current:
            recommended = max(1, current - reduction_by)
        if recommended is not None and current is not None:
            reduction_by = max(0, current - recommended)

        should_reduce = bool(current is not None and recommended is not None and recommended < current)
        return {
            "current_parallel_branches": current,
            "recommended_parallel_branches": recommended,
            "reduce_by": reduction_by if reduction_by is not None else 0,
            "should_reduce": should_reduce,
            "reason": str(section.get("reason") or section.get("summary") or "").strip(),
            "confidence": self._safe_float(section.get("confidence")) or 0.0,
        }

    def _build_annotation(self, bridge_payload: dict[str, Any], task: Any, routing_hints: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "applied",
            "bridge_source": self._bridge_source,
            "task_type": self._task_type(task),
            "context_coverage": self._normalize_context_coverage(bridge_payload, task),
            "cost_downgrade": self._normalize_cost_downgrade(bridge_payload),
            "parallelism": self._normalize_parallelism(bridge_payload, routing_hints),
        }

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        task_type = self._task_type(task)
        if task_type not in _SUPPORTED_TASK_TYPES:
            self._skipped_total += 1
            context["socraticode"] = {"status": "skipped", "task_type": task_type or None}
            return

        routing_hints = self._ensure_routing_hints(task)
        bridge, source = self._resolve_bridge()
        self._bridge_available = bridge is not None
        self._bridge_source = source

        if bridge is None:
            annotation = {"status": "unavailable", "bridge_source": None, "task_type": task_type}
            routing_hints["socraticode"] = annotation
            context["socraticode"] = annotation
            return

        try:
            bridge_payload = self._invoke_bridge(bridge, task, context)
            annotation = self._build_annotation(bridge_payload, task, routing_hints)
        except Exception as exc:
            self._failures_total += 1
            self._last_error = str(exc)
            annotation = {
                "status": "error",
                "bridge_source": self._bridge_source,
                "task_type": task_type,
                "error": str(exc),
            }
            routing_hints["socraticode"] = annotation
            context["socraticode"] = annotation
            if self._api is not None:
                self._api.log("warning", f"[SOCRATICODE] annotation failed: {exc}")
            return

        routing_hints["socraticode"] = annotation
        routing_hints["socraticode_context_coverage"] = annotation["context_coverage"]
        routing_hints["socraticode_cost_downgrade"] = annotation["cost_downgrade"]
        routing_hints["socraticode_parallelism"] = annotation["parallelism"]

        self._annotations_total += 1
        self._last_annotation = annotation
        context["socraticode"] = annotation

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        return None

    def finalize(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "bridge_available": self._bridge_available,
            "bridge_source": self._bridge_source,
            "supported_task_types": sorted(_SUPPORTED_TASK_TYPES),
            "annotations_total": self._annotations_total,
            "skipped_total": self._skipped_total,
            "failures_total": self._failures_total,
            "last_error": self._last_error,
            "last_annotation": dict(self._last_annotation),
        }
